"""SA-80: the provider cockpit — staff only, and each panel red when it should be."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.core.cache import cache
from django.utils import timezone

from analytics.models import Availability, MetricCapability, PostMetric
from analytics.tests.conftest import make_target
from channels.models import SocialAccountStatus
from content.models import PostTargetState, ProviderState
from social_providers.models import CallKind, DeliveryStatus, ProviderCall, WebhookDelivery
from social_providers.socialapi.webhooks import count

pytestmark = pytest.mark.django_db

URL = "/api/v1/ops/cockpit/"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    cache.delete_many(["socialapi:cockpit:usage", "socialapi:cockpit:webhooks"])
    yield
    cache.delete_many(["socialapi:cockpit:usage", "socialapi:cockpit:webhooks"])


@pytest.fixture
def staff(user: Any, paid_workspace: Any) -> Any:
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    return user


def panel(client: Any, name: str) -> dict[str, Any]:
    response = client.get(URL)
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()[name]
    return body


def usage(**figures: int) -> None:
    cache.set("socialapi:cockpit:usage", {"data": figures}, 60)


@pytest.fixture
def ours(social_account: Any) -> Any:
    social_account.provider_key = "socialapi"
    social_account.save(update_fields=["provider_key"])
    return social_account


# -----------------------------------------------------------------------------
# Who may see it
# -----------------------------------------------------------------------------
def test_a_member_who_is_not_staff_gets_not_found(auth_client: Any, paid_workspace: Any) -> None:
    assert auth_client.get(URL).status_code == 404


def test_signed_out_is_refused(paid_workspace: Any) -> None:
    from rest_framework.test import APIClient

    assert APIClient().get(URL).status_code == 401


def test_staff_see_every_panel_quiet_on_a_quiet_system(auth_client: Any, staff: Any) -> None:
    body = auth_client.get(URL).json()

    assert set(body) == {
        "generated_at",
        "provider",
        "accounts",
        "publishing",
        "webhooks",
        "capture",
        "audience",
        "rate_budget",
    }
    for name in ("accounts", "publishing", "capture", "audience", "rate_budget"):
        assert body[name]["alerts"] == [], name
    # Not a provider outage: the test settings carry no key, and the panel says so.
    assert "no key" in body["provider"]["usage_error"]


# -----------------------------------------------------------------------------
# Provider
# -----------------------------------------------------------------------------
def test_the_plan_running_out_turns_the_provider_red(auth_client: Any, staff: Any) -> None:
    usage(posts_used=9, posts_limit=10, interactions_used=10, interactions_limit=50)

    provider = panel(auth_client, "provider")

    assert provider["usage"]["posts_used"] == 9
    assert provider["alerts"] == ["9 of 10 posts used this period."]


def test_errors_are_grouped_with_their_last_request_id(auth_client: Any, staff: Any) -> None:
    for request_id in ("req_1", "req_2"):
        ProviderCall.objects.create(
            provider="socialapi",
            method="GET",
            path="/v1/usage",
            call_class="read",
            priority="background",
            status_code=401,
            kind=CallKind.MISCONFIGURED,
            error_code="auth.invalid_key",
            request_id=request_id,
        )

    provider = panel(auth_client, "provider")

    assert provider["errors_24h"] == [
        {
            "kind": "MISCONFIGURED",
            "code": "auth.invalid_key",
            "count": 2,
            "last_request_id": "req_2",
        }
    ]
    assert "Our key or setup was refused in the last 24 h." in provider["alerts"]


# -----------------------------------------------------------------------------
# Accounts
# -----------------------------------------------------------------------------
def test_an_account_needing_a_reconnect_is_named(auth_client: Any, staff: Any, ours: Any) -> None:
    ours.status = SocialAccountStatus.REAUTH_REQUIRED
    ours.reconnect_reason = "Access was revoked."
    ours.save(update_fields=["status", "reconnect_reason"])

    accounts = panel(auth_client, "accounts")

    assert accounts["accounts"][0]["reconnect_reason"] == "Access was revoked."
    assert len(accounts["alerts"]) == 1 and "needs reconnecting" in accounts["alerts"][0]


def test_accounts_of_the_other_provider_are_not_listed(
    auth_client: Any, staff: Any, social_account: Any
) -> None:
    social_account.provider_key = "zernio"
    social_account.save(update_fields=["provider_key"])

    assert panel(auth_client, "accounts")["accounts"] == []


# -----------------------------------------------------------------------------
# Publishing
# -----------------------------------------------------------------------------
def test_a_publish_unconfirmed_for_ten_minutes_is_stuck(
    auth_client: Any, staff: Any, paid_workspace: Any, user: Any, ours: Any
) -> None:
    target = make_target(paid_workspace, user, ours, provider_post_id="post_1")
    target.state = PostTargetState.PUBLISHING
    target.provider_state = ProviderState.PUBLISH_REQUESTED
    target.provider_key = "socialapi"
    target.provider_state_at = timezone.now() - dt.timedelta(minutes=11)
    target.save()

    publishing = panel(auth_client, "publishing")

    assert publishing["in_flight"] == 1
    assert publishing["stuck"][0]["provider_post_id"] == "post_1"
    assert any("unconfirmed" in alert for alert in publishing["alerts"])


def test_a_failure_rate_over_five_percent_is_red(
    auth_client: Any, staff: Any, paid_workspace: Any, user: Any, ours: Any
) -> None:
    for n in range(3):
        target = make_target(paid_workspace, user, ours, provider_post_id=f"p{n}")
        target.provider_key = "socialapi"
        if n == 0:
            target.state = PostTargetState.FAILED
            target.error_detail = {"code": "publish_invalid"}
        target.save()

    publishing = panel(auth_client, "publishing")

    assert (publishing["published_7d"], publishing["failed_7d"]) == (2, 1)
    assert publishing["failures_by_code_7d"] == {"publish_invalid": 1}
    assert "33% of this week's publishes failed." in publishing["alerts"]


# -----------------------------------------------------------------------------
# Webhooks
# -----------------------------------------------------------------------------
def test_without_a_secret_the_webhooks_panel_says_polling(
    auth_client: Any, staff: Any, settings: Any
) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = ""

    assert panel(auth_client, "webhooks")["alerts"] == [
        "No webhook secret: every event arrives by polling."
    ]


def test_published_posts_with_no_webhook_is_red(
    auth_client: Any, staff: Any, settings: Any, paid_workspace: Any, user: Any, ours: Any
) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = "s"
    target = make_target(paid_workspace, user, ours, age_days=0)
    target.provider_key = "socialapi"
    target.save(update_fields=["provider_key"])
    count("refused_signature")

    webhooks = panel(auth_client, "webhooks")

    assert "Posts were published in the last 24 h but no webhook arrived." in webhooks["alerts"]
    assert any("signature" in alert for alert in webhooks["alerts"])


def test_deliveries_are_counted_by_event_and_status(
    auth_client: Any, staff: Any, settings: Any
) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = "s"
    WebhookDelivery.objects.create(
        provider="socialapi", delivery_id="d1", event="post.published", status="PROCESSED"
    )
    WebhookDelivery.objects.create(
        provider="socialapi", delivery_id="d2", event="comment.received", status="FAILED"
    )

    webhooks = panel(auth_client, "webhooks")

    assert webhooks["by_event_24h"] == {"post.published": 1, "comment.received": 1}
    assert webhooks["by_status_24h"] == {"PROCESSED": 1, DeliveryStatus.FAILED: 1}
    assert "Some deliveries failed processing in the last 24 h." in webhooks["alerts"]


# -----------------------------------------------------------------------------
# Capture
# -----------------------------------------------------------------------------
def test_a_promised_metric_measuring_nothing_for_48_hours_is_red(
    auth_client: Any, staff: Any, paid_workspace: Any, user: Any, ours: Any
) -> None:
    MetricCapability.objects.create(
        provider_key="socialapi", platform="instagram", metric_key="reach", available=True
    )
    MetricCapability.objects.create(
        provider_key="socialapi", platform="instagram", metric_key="likes", available=True
    )
    target = make_target(paid_workspace, user, ours)
    PostMetric.objects.create(
        post_target=target,
        captured_at=timezone.now() - dt.timedelta(hours=2),
        likes=4,
        reach=None,
        availability=Availability.MEASURED,
        provider_key="socialapi",
    )

    capture = panel(auth_client, "capture")

    assert capture["coverage_7d"]["instagram"]["likes"] == {"rows": 1, "measured": 1}
    assert capture["coverage_7d"]["instagram"]["reach"] == {"rows": 1, "measured": 0}
    assert capture["alerts"] == [
        "instagram reach is marked available but measured nothing in 48 h."
    ]


# -----------------------------------------------------------------------------
# Audience and the rate budget
# -----------------------------------------------------------------------------
def test_the_vendors_interactions_running_out_is_red(auth_client: Any, staff: Any) -> None:
    usage(interactions_used=45, interactions_limit=50)

    audience = panel(auth_client, "audience")

    assert audience["provider_interactions"] == {"used": 45, "limit": 50}
    assert audience["alerts"] == ["45 of 50 provider interactions used this period."]


def test_a_publish_waiting_on_the_budget_is_red(auth_client: Any, staff: Any) -> None:
    ProviderCall.objects.create(
        provider="socialapi",
        method="POST",
        path="/v1/posts",
        call_class="write",
        priority="publish",
        kind=CallKind.RATE_LIMITED,
        local=True,
    )

    budget = panel(auth_client, "rate_budget")

    assert budget["refused_last_hour"] == {"publish": 1}
    assert budget["alerts"] == ["A publish waited on the rate budget in the last hour."]
