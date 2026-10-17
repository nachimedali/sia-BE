"""Step 3 of the provider plan (SA-20, SA-21): the signed connect state, the
provider stamp, disconnect and the fate of scheduled posts, and the two reads
the connections page draws from (`/channels/overview/`, `/channels/shares/`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import time_machine
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from analytics.models import AccountSnapshot, MetricCapability
from channels import services
from channels.models import SocialAccount, SocialAccountStatus
from content.models import DeliveryMode, Post, PostStatus, PostTarget
from content.services.posts import create_post
from scheduling.services import schedule_post
from workspaces.models import Membership, Role
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

COMPLETE_URL = "/api/v1/channels/complete/"


def _client(user: Any) -> APIClient:
    api = APIClient()
    api.force_authenticate(user)
    return api


def _member(workspace: Any, role: str, email: str) -> Any:
    person = get_user_model().objects.create_user(email=email, password="x-Pass-1234")
    Membership.objects.create(user=person, workspace=workspace, role=role)
    return person


def _scheduled(workspace: Any, user: Any, *, in_hours: int = 24) -> Post:
    post = create_post(workspace=workspace, author=user, master_body="Spring drop")
    return schedule_post(
        post=post,
        delivery_mode=DeliveryMode.AUTO_PUBLISH,
        scheduled_at=timezone.now() + dt.timedelta(hours=in_hours),
        actor=user,
    )


def _second_account(workspace: Any) -> SocialAccount:
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        handle="@acme-fb",
        provider_account_id="acct-facebook-1",
    )


# -----------------------------------------------------------------------------
# The signed state
# -----------------------------------------------------------------------------
def test_connect_signs_a_state_naming_workspace_person_and_platform(
    auth_client: Any, paid_workspace: Any, user: Any, platform_adapter: Any
) -> None:
    response = auth_client.get("/api/v1/channels/instagram/connect/")

    assert response.status_code == 200
    state = platform_adapter.connect_urls[0]["state"]
    assert state
    assert parse_qs(urlparse(response.json()["auth_url"]).query)["state"] == [state]
    services.verify_state(state, workspace=paid_workspace, user=user, platform="instagram")


def test_a_return_carrying_the_state_finishes(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    state = services.sign_state(workspace=paid_workspace, user=user, platform="instagram")

    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 200
    assert SocialAccount.objects.filter(workspace=paid_workspace).count() == 1


def test_a_state_minted_for_another_platform_is_refused(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    state = services.sign_state(workspace=paid_workspace, user=user, platform="tiktok")

    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_invalid"
    assert not SocialAccount.objects.exists()


def test_a_state_started_in_another_workspace_cannot_finish_here(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    """The cross-workspace guard: a link started in brand A must not attach
    an account to brand B because the browser came back with B selected."""
    other = provision_extra_workspace(user=user, name="Second Brand")
    state = services.sign_state(workspace=other, user=user, platform="instagram")

    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
        HTTP_X_WORKSPACE_ID=str(paid_workspace.pk),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_invalid"


def test_a_state_started_by_another_person_cannot_finish(paid_workspace: Any, user: Any) -> None:
    colleague = _member(paid_workspace, Role.ADMIN, "colleague@example.com")
    state = services.sign_state(workspace=paid_workspace, user=user, platform="instagram")

    response = _client(colleague).post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_invalid"


def test_a_forged_state_is_refused(auth_client: Any, paid_workspace: Any) -> None:
    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": "x:y:z"}},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_invalid"


def test_a_state_older_than_thirty_minutes_says_start_again(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    with time_machine.travel(timezone.now() - dt.timedelta(minutes=31)):
        state = services.sign_state(workspace=paid_workspace, user=user, platform="instagram")

    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_expired"


def test_an_adapter_that_always_echoes_state_requires_it(
    auth_client: Any, paid_workspace: Any, platform_adapter: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(platform_adapter, "requires_state", True, raising=False)

    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "c"}},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "connect_state_invalid"


# -----------------------------------------------------------------------------
# What attaching records
# -----------------------------------------------------------------------------
def test_an_attached_account_records_which_provider_connected_it(
    auth_client: Any, paid_workspace: Any
) -> None:
    auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}},
        format="json",
    )

    assert SocialAccount.objects.get().provider_key == "fake"


def test_reconnecting_clears_the_reconnect_reason(auth_client: Any, paid_workspace: Any) -> None:
    payload = {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}}
    auth_client.post(COMPLETE_URL, payload, format="json")
    SocialAccount.objects.update(
        status=SocialAccountStatus.REAUTH_REQUIRED, reconnect_reason="Token expired"
    )

    auth_client.post(COMPLETE_URL, payload, format="json")

    account = SocialAccount.objects.get()
    assert account.status == SocialAccountStatus.ACTIVE
    assert account.reconnect_reason == ""


def test_reconnecting_an_account_already_here_does_not_count_against_the_cap(
    auth_client: Any, paid_workspace: Any
) -> None:
    payload = {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}}
    auth_client.post(COMPLETE_URL, payload, format="json")
    SocialAccount.objects.bulk_create(
        SocialAccount(workspace=paid_workspace, platform="threads", provider_account_id=f"f-{i}")
        for i in range(4)
    )
    SocialAccount.objects.filter(provider_account_id="acct-instagram-ig-1").update(
        status=SocialAccountStatus.REAUTH_REQUIRED
    )

    connected = SocialAccount.objects.get(platform="instagram")
    again = services.attach_account(
        workspace=paid_workspace,
        platform="instagram",
        connected=type(
            "C",
            (),
            {
                "provider_account_id": connected.provider_account_id,
                "handle": "@x",
                "display_name": "X",
                "followers": 1,
            },
        )(),
    )

    assert again.pk == connected.pk
    assert again.status == SocialAccountStatus.ACTIVE


# -----------------------------------------------------------------------------
# Disconnect — impact, admins only, and the fate of scheduled posts
# -----------------------------------------------------------------------------
def test_impact_lists_posts_that_would_lose_their_only_destination(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = _scheduled(paid_workspace, user)

    response = auth_client.get(f"/api/v1/channels/{social_account.pk}/impact/")

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["only_here"]] == [post.pk]
    assert body["also_elsewhere"] == []


def test_disconnect_turns_only_here_posts_into_reminders_at_the_same_time(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    post = _scheduled(paid_workspace, user)
    when = post.scheduled_at

    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/", {"scheduled": "remind"}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["reminded"] == 1
    post.refresh_from_db()
    assert post.delivery_mode == DeliveryMode.REMINDER
    assert post.scheduled_at == when
    assert not PostTarget.objects.filter(post=post, social_account=social_account).exists()
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.REVOKED
    assert platform_adapter.disconnected == ["acct-instagram-1"]


def test_disconnect_can_take_only_here_posts_off_the_calendar_with_approval_kept(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = _scheduled(paid_workspace, user)

    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/",
        {"scheduled": "unschedule"},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["unscheduled"] == 1
    post.refresh_from_db()
    assert post.status == PostStatus.APPROVED
    assert post.scheduled_at is None


def test_posts_that_also_go_elsewhere_keep_publishing_there(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    other = _second_account(paid_workspace)
    post = _scheduled(paid_workspace, user)
    assert PostTarget.objects.filter(post=post).count() == 2

    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/", {"scheduled": "remind"}, format="json"
    )

    assert response.json()["kept_elsewhere"] == 1
    post.refresh_from_db()
    assert post.status == PostStatus.SCHEDULED
    assert post.delivery_mode == DeliveryMode.AUTO_PUBLISH
    assert list(PostTarget.objects.filter(post=post).values_list("social_account", flat=True)) == [
        other.pk
    ]


def test_a_provider_refusal_changes_nothing_locally(
    auth_client: Any,
    paid_workspace: Any,
    user: Any,
    social_account: Any,
    platform_adapter: Any,
    monkeypatch: Any,
) -> None:
    from channels.adapters.base import PlatformError

    post = _scheduled(paid_workspace, user)

    def refuse(**_: Any) -> None:
        raise PlatformError("provider down")

    monkeypatch.setattr(platform_adapter, "disconnect", refuse)

    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/", {"scheduled": "remind"}, format="json"
    )

    assert response.status_code == 422
    social_account.refresh_from_db()
    post.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE
    assert post.delivery_mode == DeliveryMode.AUTO_PUBLISH


def test_an_unknown_fate_is_a_400(auth_client: Any, social_account: Any) -> None:
    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/", {"scheduled": "drop"}, format="json"
    )

    assert response.status_code == 400
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE


def test_only_admins_disconnect(paid_workspace: Any, social_account: Any) -> None:
    editor = _member(paid_workspace, Role.EDITOR, "editor@example.com")

    response = _client(editor).post(f"/api/v1/channels/{social_account.pk}/disconnect/")

    assert response.status_code == 403
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE


def test_another_tenants_account_is_404_for_impact_and_disconnect(
    other_user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    theirs = provision_workspace(other_user, name="Not Yours")
    # Paid, so the answer is about tenancy and not about the plan's 402.
    theirs.organization.plan = plans["pro"]
    theirs.organization.save(update_fields=["plan"])
    stranger = _client(other_user)

    assert stranger.get(f"/api/v1/channels/{social_account.pk}/impact/").status_code == 404
    assert stranger.post(f"/api/v1/channels/{social_account.pk}/disconnect/").status_code == 404


def test_another_workspace_of_the_same_org_is_404_too(
    auth_client: Any, user: Any, social_account: Any
) -> None:
    second = provision_extra_workspace(user=user, name="Second Brand")

    response = auth_client.post(
        f"/api/v1/channels/{social_account.pk}/disconnect/",
        HTTP_X_WORKSPACE_ID=str(second.pk),
    )

    assert response.status_code == 404


# -----------------------------------------------------------------------------
# The overview the page draws
# -----------------------------------------------------------------------------
def test_an_account_never_captured_is_syncing_not_connected(
    auth_client: Any, social_account: Any
) -> None:
    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["state"] == "syncing"
    assert card["last_synced_at"] is None
    assert card["followers_change_7d"] is None


def test_followers_change_is_null_without_a_snapshot_from_a_week_ago(
    auth_client: Any, social_account: Any
) -> None:
    now = timezone.now()
    AccountSnapshot.objects.create(
        social_account=social_account, captured_at=now - dt.timedelta(days=2), followers=900
    )
    AccountSnapshot.objects.create(social_account=social_account, captured_at=now, followers=950)

    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["state"] == "connected"
    assert card["followers"] == 950
    assert card["followers_change_7d"] is None, "no week-old snapshot is not a change of 0"


def test_followers_change_is_the_difference_from_a_week_ago(
    auth_client: Any, social_account: Any
) -> None:
    now = timezone.now()
    AccountSnapshot.objects.create(
        social_account=social_account, captured_at=now - dt.timedelta(days=8), followers=900
    )
    AccountSnapshot.objects.create(social_account=social_account, captured_at=now, followers=950)

    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["followers_change_7d"] == 50


def test_cards_count_what_depends_on_the_account(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    _scheduled(paid_workspace, user)

    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["scheduled"] == 1
    assert card["published_30d"] == 0


def test_states_follow_the_account_status(auth_client: Any, social_account: Any) -> None:
    for status, state in (
        (SocialAccountStatus.REAUTH_REQUIRED, "reconnect"),
        (SocialAccountStatus.PAGE_REMOVED, "removed"),
        (SocialAccountStatus.OVER_LIMIT, "over"),
        (SocialAccountStatus.REVOKED, "disconnected"),
    ):
        SocialAccount.objects.filter(pk=social_account.pk).update(status=status)
        card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]
        assert card["state"] == state, status


def test_the_cap_is_pooled_across_the_organizations_workspaces(
    auth_client: Any, user: Any, paid_workspace: Any, social_account: Any
) -> None:
    second = provision_extra_workspace(user=user, name="Second Brand")
    SocialAccount.objects.create(workspace=second, platform="tiktok", provider_account_id="t-1")

    body = auth_client.get(
        "/api/v1/channels/overview/", HTTP_X_WORKSPACE_ID=str(paid_workspace.pk)
    ).json()

    assert body["cap"]["used"] == 2
    assert body["cap"]["limit"] == 5
    assert {row["name"]: row["current"] for row in body["cap"]["by_workspace"]} == {
        paid_workspace.name: True,
        "Second Brand": False,
    }
    assert body["can_manage"] is True


def test_a_non_admin_sees_the_overview_but_cannot_manage(
    paid_workspace: Any, social_account: Any
) -> None:
    editor = _member(paid_workspace, Role.EDITOR, "editor@example.com")

    body = _client(editor).get("/api/v1/channels/overview/").json()

    assert len(body["cards"]) == 1
    assert body["can_manage"] is False


def test_the_overview_never_shows_another_workspaces_accounts(
    auth_client: Any, user: Any, paid_workspace: Any, social_account: Any
) -> None:
    second = provision_extra_workspace(user=user, name="Second Brand")

    body = auth_client.get("/api/v1/channels/overview/", HTTP_X_WORKSPACE_ID=str(second.pk)).json()

    assert body["cards"] == []


# -----------------------------------------------------------------------------
# What each platform shares
# -----------------------------------------------------------------------------
def test_shares_come_from_the_selected_providers_capability_rows(
    auth_client: Any, paid_workspace: Any, settings: Any
) -> None:
    settings.SOCIAL_PROVIDER = "socialapi"
    MetricCapability.objects.create(
        provider_key="socialapi",
        platform="instagram",
        metric_key="reach",
        available=True,
        latency_hint=dt.timedelta(hours=48),
    )
    MetricCapability.objects.create(
        provider_key="socialapi",
        platform="facebook",
        metric_key="reach",
        available=False,
        note="Facebook Page insights are still in beta at the provider",
    )

    body = auth_client.get("/api/v1/channels/shares/").json()

    assert body["provider"] == "socialapi"
    ig = body["platforms"]["instagram"]
    assert ig["metrics"]["reach"] == {"available": True, "delay_hours": 48, "note": ""}
    assert ig["comments"] is True
    assert "professional account" in ig["requires"]
    fb = body["platforms"]["facebook"]
    assert fb["metrics"]["reach"]["available"] is False
    assert "beta" in fb["metrics"]["reach"]["note"]
    assert fb["selection"] is True
    assert body["platforms"]["tiktok"]["comments"] is False
    x = body["platforms"]["x"]
    assert x["connectable"] is False and "developer app" in x["unavailable_reason"]


def test_an_undeclared_metric_is_not_promised(auth_client: Any, paid_workspace: Any) -> None:
    MetricCapability.objects.all().delete()

    body = auth_client.get("/api/v1/channels/shares/").json()

    views = body["platforms"]["youtube"]["metrics"]["views"]
    assert views["available"] is False
    assert views["note"]


# -----------------------------------------------------------------------------
# The return names its platform through the state; a reconnect holds its slot
# -----------------------------------------------------------------------------
def test_a_return_without_a_platform_takes_it_from_the_signed_state(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    """SocialAPI comes back with only its own params (`status`, `account_id`,
    `state`); the platform is read from the state we signed."""
    state = services.sign_state(workspace=paid_workspace, user=user, platform="instagram")

    response = auth_client.post(
        COMPLETE_URL,
        {"params": {"accountId": "ig-1", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 200
    assert SocialAccount.objects.get().platform == "instagram"


def test_a_return_with_neither_platform_nor_state_is_a_400(
    auth_client: Any, paid_workspace: Any
) -> None:
    response = auth_client.post(
        COMPLETE_URL, {"params": {"accountId": "ig-1", "code": "c"}}, format="json"
    )

    assert response.status_code == 400
    assert not SocialAccount.objects.exists()


def test_a_reconnect_finishes_even_at_the_cap(
    auth_client: Any, paid_workspace: Any, user: Any, platform_adapter: Any
) -> None:
    auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"accountId": "ig-1", "code": "abc"}},
        format="json",
    )
    account = SocialAccount.objects.get()
    SocialAccount.objects.filter(pk=account.pk).update(status=SocialAccountStatus.REAUTH_REQUIRED)
    SocialAccount.objects.bulk_create(
        SocialAccount(workspace=paid_workspace, platform="threads", provider_account_id=f"f-{i}")
        for i in range(4)
    )

    auth_client.post(f"/api/v1/channels/{account.pk}/reauth/")
    state = platform_adapter.connect_urls[-1]["state"]
    assert services.read_state(state)["r"] == account.pk

    response = auth_client.post(
        COMPLETE_URL,
        {"params": {"accountId": "ig-1", "code": "abc", "state": state}},
        format="json",
    )

    assert response.status_code == 200
    account.refresh_from_db()
    assert account.status == SocialAccountStatus.ACTIVE


def test_a_new_connect_at_the_cap_is_still_refused(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    SocialAccount.objects.bulk_create(
        SocialAccount(workspace=paid_workspace, platform="threads", provider_account_id=f"f-{i}")
        for i in range(5)
    )
    state = services.sign_state(workspace=paid_workspace, user=user, platform="instagram")

    response = auth_client.post(
        COMPLETE_URL,
        {"params": {"accountId": "ig-9", "code": "c", "state": state}},
        format="json",
    )

    assert response.status_code == 402


def test_a_refused_sign_in_attaches_nothing(auth_client: Any, paid_workspace: Any) -> None:
    response = auth_client.post(
        COMPLETE_URL,
        {"platform": "instagram", "params": {"error": "access_denied"}},
        format="json",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "connect_denied"
    assert not SocialAccount.objects.exists()
