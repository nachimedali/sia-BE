"""Connect flow and the account cap (design.md §6.2, §7, I6, D4).

The headless flow is exercised end to end through the API, both shapes: the
platforms that connect in one leg (Instagram) and the ones that make the user
pick a page or organisation first (Facebook, LinkedIn — `SELECTION_PLATFORMS`).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.admin.sites import AdminSite
from django.http import HttpRequest

from billing.services.subscriptions import downgrade_to_free
from channels import services
from channels.admin import SocialAccountAdmin
from channels.models import SocialAccount, SocialAccountStatus

pytestmark = pytest.mark.django_db


def _connect_url(platform: str) -> str:
    return f"/api/v1/channels/{platform}/connect/"


COMPLETE_URL = "/api/v1/channels/complete/"


def _fill_to_cap(workspace: Any, *, count: int) -> None:
    SocialAccount.objects.bulk_create(
        SocialAccount(
            workspace=workspace,
            platform="instagram",
            provider_account_id=f"filler-{i}",
            handle=f"@filler{i}",
        )
        for i in range(count)
    )


# -----------------------------------------------------------------------------
# Connect
# -----------------------------------------------------------------------------
def test_connect_returns_a_provider_url_and_mints_a_profile(
    auth_client: Any, paid_workspace: Any, platform_adapter: Any
) -> None:
    response = auth_client.get(_connect_url("instagram"))

    assert response.status_code == 200
    assert response.json()["auth_url"].startswith("https://fake-platform.test/oauth/instagram")

    paid_workspace.refresh_from_db()
    assert paid_workspace.provider_profile_id == "fake-profile-1"
    # The redirect lands on our own site, never on the provider (D2).
    assert platform_adapter.connect_urls[0]["redirect_url"].endswith("/app/settings/connections")


def test_connect_reuses_an_existing_provider_profile(
    auth_client: Any, paid_workspace: Any, platform_adapter: Any
) -> None:
    auth_client.get(_connect_url("instagram"))
    auth_client.get(_connect_url("threads"))

    assert platform_adapter.profiles == ["acme-studio"], "profile minted more than once"


def test_connect_rejects_an_unsupported_platform(auth_client: Any, paid_workspace: Any) -> None:
    response = auth_client.get(_connect_url("myspace"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_platform"


def test_connect_rejects_an_off_site_redirect_path(auth_client: Any, paid_workspace: Any) -> None:
    response = auth_client.get(
        _connect_url("instagram"), {"redirect_path": "https://evil.test/steal"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_redirect"


def test_complete_attaches_an_account_without_a_selection_step(
    auth_client: Any, paid_workspace: Any
) -> None:
    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needs_selection"] is False
    assert body["account"]["handle"] == "@instagram-abc"

    account = SocialAccount.objects.get(workspace=paid_workspace)
    assert account.status == SocialAccountStatus.ACTIVE
    assert account.followers_cached == 1234


def test_complete_offers_targets_then_attaches_the_chosen_one(
    auth_client: Any, paid_workspace: Any
) -> None:
    """Facebook publishes as a Page, so the first leg can only offer choices.
    Rendering them is OCCS's job under the headless flow (design.md §14, V1)."""
    first = auth_client.post(
        COMPLETE_URL,
        {"platform": "facebook", "params": {"tempToken": "t-1"}},
        format="json",
    )

    assert first.status_code == 200
    offered = first.json()
    assert offered["needs_selection"] is True
    assert [t["id"] for t in offered["targets"]] == ["facebook-target-1", "facebook-target-2"]
    assert not SocialAccount.objects.exists(), "nothing may be attached before a choice"

    second = auth_client.post(
        COMPLETE_URL,
        {
            "platform": "facebook",
            "params": offered["params"],
            "target_id": "facebook-target-2",
        },
        format="json",
    )

    assert second.status_code == 200
    assert second.json()["account"]["handle"] == "@facebook-facebook-target-2"
    assert SocialAccount.objects.get().provider_account_id == "acct-facebook-facebook-target-2"


def test_completing_the_same_account_twice_refreshes_rather_than_duplicates(
    auth_client: Any, paid_workspace: Any
) -> None:
    payload = {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}}
    auth_client.post(COMPLETE_URL, payload, format="json")
    auth_client.post(COMPLETE_URL, payload, format="json")

    assert SocialAccount.objects.count() == 1


def test_free_tier_cannot_reach_the_connect_flow(auth_client: Any, workspace: Any) -> None:
    """D4: auto-publish is the paid path, and connecting an account is its
    first step. A 402 with an upgrade payload, not a 403 (design.md A2)."""
    response = auth_client.get(_connect_url("instagram"))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]["suggested_plan"] == "pro"


# -----------------------------------------------------------------------------
# Disconnect / reauth
# -----------------------------------------------------------------------------
def test_disconnect_tells_the_provider_and_marks_revoked(
    auth_client: Any, social_account: Any, platform_adapter: Any
) -> None:
    response = auth_client.post(f"/api/v1/channels/{social_account.pk}/disconnect/")

    assert response.status_code == 200
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.REVOKED
    assert platform_adapter.disconnected == ["acct-instagram-1"]
    # I10: the row survives, so the posts that went through it still resolve.
    assert SocialAccount.objects.filter(pk=social_account.pk).exists()


def test_reauth_returns_a_fresh_connect_url_even_at_the_cap(
    auth_client: Any, paid_workspace: Any, social_account: Any
) -> None:
    _fill_to_cap(paid_workspace, count=4)  # 4 + social_account = Pro's cap of 5

    response = auth_client.post(f"/api/v1/channels/{social_account.pk}/reauth/")

    assert response.status_code == 200
    assert "auth_url" in response.json()


# -----------------------------------------------------------------------------
# test_account_cap_enforced_at_all_three_layers (I6)
# -----------------------------------------------------------------------------
def test_account_cap_enforced_at_all_three_layers(
    auth_client: Any, paid_workspace: Any, user: Any, platform_adapter: Any
) -> None:
    """Each layer is checked with the other two bypassed, so "the serializer
    would have caught it" is never an acceptable answer (mirrors the I5 gate
    test's shape in ai/tests/test_entitlement_gates.py).

    Pro allows 5 (`seed_plans`); this fills all 5 and then tries a sixth.
    """
    _fill_to_cap(paid_workspace, count=5)

    # Layer 1 — the serializer, reached through the API.
    api = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-6", "code": "six"}},
        format="json",
    )
    assert api.status_code == 402
    assert api.json()["error"]["code"] == "quota_exceeded"
    assert platform_adapter.connect_urls == [], "refused before any provider call"

    # Layer 2 — the admin, which bypasses the serializer entirely.
    admin = SocialAccountAdmin(SocialAccount, AdminSite())
    sixth = SocialAccount(
        workspace=paid_workspace, platform="threads", provider_account_id="admin-6"
    )
    with pytest.raises(Exception, match="allows 5"):
        admin.save_model(HttpRequest(), sixth, None, change=False)

    # Layer 3 — publish time, which neither of the above can reach. The cap
    # moved under accounts that were legitimately connected, so the newest are
    # parked and the parked one refuses to publish.
    over_cap = SocialAccount.objects.order_by("connected_at", "pk").last()
    assert over_cap is not None
    downgrade_to_free(paid_workspace, reason="test")
    with pytest.raises(services.AccountNotPublishableError):
        services.ensure_publishable(over_cap)

    assert SocialAccount.objects.filter(status=SocialAccountStatus.ACTIVE).count() == 1
    assert SocialAccount.objects.filter(status=SocialAccountStatus.OVER_LIMIT).count() == 4


def test_downgrade_parks_the_newest_accounts_and_keeps_the_oldest(
    paid_workspace: Any, social_account: Any
) -> None:
    """I10: mark, never delete — and the *oldest* connection survives, because
    that is the one the workspace has been publishing from."""
    _fill_to_cap(paid_workspace, count=3)

    downgrade_to_free(paid_workspace, reason="trial expired")

    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE
    assert SocialAccount.objects.filter(status=SocialAccountStatus.OVER_LIMIT).count() == 3
    assert SocialAccount.objects.count() == 4


def test_admin_saves_a_first_account_within_the_cap(paid_workspace: Any) -> None:
    """The other side of layer two: the guard must not refuse the ordinary
    case it is wrapped around."""
    admin = SocialAccountAdmin(SocialAccount, AdminSite())
    account = SocialAccount(
        workspace=paid_workspace, platform="threads", provider_account_id="admin-1"
    )

    admin.save_model(HttpRequest(), account, None, change=False)

    assert SocialAccount.objects.filter(provider_account_id="admin-1").exists()
