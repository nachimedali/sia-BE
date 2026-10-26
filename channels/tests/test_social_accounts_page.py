"""The social accounts page (`/app/social-accounts`): what each card adds to
the overview — where the account was published on and when last — and the
"Check status" read (`POST /channels/{id}/check/`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from channels.adapters.base import PlatformError
from channels.models import SocialAccount, SocialAccountStatus
from content.models import Post, PostStatus, PostTarget, PostTargetState
from content.services.posts import create_post
from notifications.models import Notification
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db


def _check(client: Any, account: Any, **headers: Any) -> Any:
    return client.post(f"/api/v1/channels/{account.pk}/check/", **headers)


def _published(workspace: Any, user: Any, account: Any, *, body: str, days_ago: int) -> Any:
    post = create_post(workspace=workspace, author=user, master_body=body)
    Post.objects.filter(pk=post.pk).update(status=PostStatus.PUBLISHED)
    return PostTarget.objects.create(
        post=post,
        platform=account.platform,
        social_account=account,
        state=PostTargetState.PUBLISHED,
        published_at=timezone.now() - dt.timedelta(days=days_ago),
    )


# -----------------------------------------------------------------------------
# Published on, last published
# -----------------------------------------------------------------------------
def test_a_card_with_nothing_published_says_so(auth_client: Any, social_account: Any) -> None:
    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["published_total"] == 0
    assert card["last_published_at"] is None
    assert card["last_published_post"] is None


def test_a_card_counts_every_publish_and_names_the_latest(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    _published(paid_workspace, user, social_account, body="Old one", days_ago=60)
    latest = _published(paid_workspace, user, social_account, body="Spring drop", days_ago=2)

    card = auth_client.get("/api/v1/channels/overview/").json()["cards"][0]

    assert card["published_total"] == 2
    assert card["published_30d"] == 1
    assert card["last_published_post"] == latest.post_id
    assert card["last_published_at"] is not None


def test_another_accounts_publishes_are_not_counted(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    other = SocialAccount.objects.create(
        workspace=paid_workspace, platform="facebook", provider_account_id="fb-1"
    )
    _published(paid_workspace, user, other, body="Elsewhere", days_ago=1)

    body = auth_client.get("/api/v1/channels/overview/").json()
    cards = {card["account"]["id"]: card for card in body["cards"]}

    assert cards[social_account.pk]["published_total"] == 0
    assert cards[other.pk]["published_total"] == 1


# -----------------------------------------------------------------------------
# Check status
# -----------------------------------------------------------------------------
def test_check_keeps_an_account_the_provider_still_holds(
    auth_client: Any, social_account: Any, platform_adapter: Any
) -> None:
    response = _check(auth_client, social_account)

    assert response.status_code == 200
    body = response.json()
    assert body["card"]["state"] == "syncing"
    assert body["present"] is True
    assert body["checked_at"]
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE


def test_check_marks_an_account_gone_at_the_provider_for_reconnect_and_tells_admins_once(
    auth_client: Any, social_account: Any, platform_adapter: Any
) -> None:
    platform_adapter.missing.add(social_account.provider_account_id)

    first = _check(auth_client, social_account)
    second = _check(auth_client, social_account)

    assert first.status_code == 200
    assert first.json()["card"]["state"] == "reconnect"
    assert first.json()["present"] is False
    assert second.json()["card"]["state"] == "reconnect"
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.REAUTH_REQUIRED
    assert social_account.reconnect_reason
    assert Notification.objects.filter(event_key="account.reconnect").count() == 1


def test_check_does_not_call_the_provider_for_an_account_already_out(
    auth_client: Any, social_account: Any, platform_adapter: Any
) -> None:
    SocialAccount.objects.filter(pk=social_account.pk).update(status=SocialAccountStatus.REVOKED)
    platform_adapter.missing.add(social_account.provider_account_id)

    response = _check(auth_client, social_account)

    assert response.status_code == 200
    assert response.json()["card"]["state"] == "disconnected"
    assert response.json()["present"] is None
    assert platform_adapter.presence_checks == []


def test_an_account_from_the_previous_provider_needs_reconnecting_without_a_call(
    auth_client: Any, social_account: Any, platform_adapter: Any
) -> None:
    SocialAccount.objects.filter(pk=social_account.pk).update(provider_key="zernio-old")

    response = _check(auth_client, social_account)

    assert response.json()["card"]["state"] == "reconnect"
    assert platform_adapter.presence_checks == []


def test_a_provider_failure_is_a_422_and_changes_nothing(
    auth_client: Any, social_account: Any, platform_adapter: Any, monkeypatch: Any
) -> None:
    def down(**_: Any) -> bool:
        raise PlatformError("SocialAPI accounts returned 503.", retryable=True)

    monkeypatch.setattr(platform_adapter, "account_present", down)

    response = _check(auth_client, social_account)

    assert response.status_code == 422
    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.ACTIVE


def test_check_is_a_paid_feature(auth_client: Any, social_account: Any, plans: Any) -> None:
    org = social_account.workspace.organization
    org.plan = plans["free"]
    org.save(update_fields=["plan"])

    response = _check(auth_client, social_account)

    assert response.status_code == 402


def test_check_on_another_organizations_account_is_404(
    social_account: Any, other_user: Any, plans: dict[str, Any]
) -> None:
    theirs = provision_workspace(other_user, name="Not Yours")
    # Paid, so the answer is about tenancy and not about the plan's 402.
    theirs.organization.plan = plans["pro"]
    theirs.organization.save(update_fields=["plan"])
    stranger = APIClient()
    stranger.force_authenticate(other_user)

    assert _check(stranger, social_account).status_code == 404


def test_check_from_another_workspace_of_the_same_org_is_404(
    auth_client: Any, user: Any, social_account: Any
) -> None:
    second = provision_extra_workspace(user=user, name="Second Brand")

    response = _check(auth_client, social_account, HTTP_X_WORKSPACE_ID=str(second.pk))

    assert response.status_code == 404
