"""SA-40/41/42: SocialAPI's webhooks, received, verified, deduplicated and routed.

Plan §10's list for the receiver — forged, replayed, stale, foreign, duplicate,
valid — plus the vendor's unverifiable first ping, a rotation, a queue that was
down, and each routed event processed twice to the same result.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import time
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from analytics.models import AudienceComment
from analytics.tests.conftest import make_target
from channels.models import SocialAccountStatus
from content.models import PostStatus, PostTargetState, ProviderState
from notifications.models import EventKey, Notification
from social_providers.models import DeliveryStatus, WebhookDelivery
from social_providers.socialapi import webhooks
from social_providers.tests.stub import MemoryJournal, ReadVendor, StubVendor

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/socialapi/"
SECRET = "whsec_" + "a" * 58
ACCOUNT = "acc_ours"


@pytest.fixture(autouse=True)
def _secret(settings: Any) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = SECRET
    settings.SOCIALAPI_WEBHOOK_SECRET_PREVIOUS = ""


@pytest.fixture
def ours(social_account: Any) -> Any:
    social_account.provider_account_id = ACCOUNT
    social_account.provider_key = "socialapi"
    social_account.save(update_fields=["provider_account_id", "provider_key"])
    return social_account


def deliver(
    event: str,
    data: dict[str, Any],
    *,
    delivery: str = "dlv_1",
    secret: str = SECRET,
    at: int | None = None,
    body: bytes | None = None,
) -> Any:
    raw = body if body is not None else json.dumps({"event": event, "data": data}).encode()
    timestamp = str(at if at is not None else int(time.time()))
    headers = {
        "X-SocialAPI-Event": event,
        "X-SocialAPI-Timestamp": timestamp,
        "X-SocialAPI-Signature-V2": webhooks.signature(secret, timestamp, raw),
    }
    if delivery:
        headers["X-SocialAPI-Delivery"] = delivery
    return Client().post(URL, data=raw, content_type="application/json", headers=headers)


def row(delivery: str = "dlv_1") -> WebhookDelivery:
    return WebhookDelivery.objects.get(delivery_id=delivery)


# -----------------------------------------------------------------------------
# The receiver (SA-40)
# -----------------------------------------------------------------------------
def test_the_unverifiable_first_ping_is_answered_and_nothing_is_kept(settings: Any) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = ""  # the secret is revealed only after the ping

    response = Client().post(
        URL,
        data=b'{"event":"webhook.test","data":{}}',
        content_type="application/json",
        headers={"X-SocialAPI-Event": "webhook.test", "X-SocialAPI-Signature-V2": "sha256=x"},
    )

    assert response.status_code == 204
    assert not WebhookDelivery.objects.exists()


def test_a_real_delivery_without_a_configured_secret_is_retryable(settings: Any) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = ""

    response = deliver("account.connected", {"account_id": ACCOUNT})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "webhook_not_configured"
    assert not WebhookDelivery.objects.exists()


def test_a_forged_signature_is_refused_before_parsing() -> None:
    response = deliver("account.connected", {"account_id": ACCOUNT}, secret="not-ours")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert not WebhookDelivery.objects.exists()
    assert webhooks.counts(timezone.now().date())["refused_signature"] >= 1


def test_a_body_changed_after_signing_is_refused() -> None:
    raw = json.dumps({"event": "page.removed", "data": {"account_id": ACCOUNT}}).encode()
    timestamp = str(int(time.time()))
    response = Client().post(
        URL,
        data=raw.replace(b"page.removed", b"post.deleted"),
        content_type="application/json",
        headers={
            "X-SocialAPI-Delivery": "dlv_1",
            "X-SocialAPI-Timestamp": timestamp,
            "X-SocialAPI-Signature-V2": webhooks.signature(SECRET, timestamp, raw),
        },
    )

    assert response.status_code == 400
    assert not WebhookDelivery.objects.exists()


def test_a_replay_outside_the_window_is_refused() -> None:
    old = int(time.time()) - 301

    response = deliver("account.connected", {"account_id": ACCOUNT}, at=old)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "stale_delivery"


def test_a_signed_body_that_is_not_an_event_is_refused() -> None:
    response = deliver("x", {}, body=b"[1, 2, 3]")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payload"


def test_a_valid_delivery_is_recorded_answered_and_processed(ours: Any) -> None:
    response = deliver("account.connected", {"account_id": ACCOUNT})

    assert response.status_code == 204
    delivery = row()
    assert delivery.event == "account.connected"
    assert delivery.status == DeliveryStatus.IGNORED
    assert delivery.attempts == 1


def test_a_retried_delivery_is_processed_once(ours: Any) -> None:
    payload = {"account_id": ACCOUNT, "reason": "access_revoked", "reconnect_required": True}

    first = deliver("account.disconnected", payload)
    second = deliver("account.disconnected", payload)

    assert (first.status_code, second.status_code) == (204, 204)
    assert WebhookDelivery.objects.count() == 1
    assert row().attempts == 1
    assert Notification.objects.filter(event_key=EventKey.ACCOUNT_RECONNECT).count() == 1


def test_the_previous_secret_still_verifies_during_a_rotation(settings: Any, ours: Any) -> None:
    settings.SOCIALAPI_WEBHOOK_SECRET = "whsec_new"
    settings.SOCIALAPI_WEBHOOK_SECRET_PREVIOUS = SECRET

    assert deliver("account.connected", {"account_id": ACCOUNT}).status_code == 204


def test_a_delivery_the_queue_never_ran_is_picked_up_by_the_sweep(
    ours: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from social_providers import tasks

    def queue_down(_delivery: Any) -> None:
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(tasks, "enqueue", queue_down)
    assert deliver("account.connected", {"account_id": ACCOUNT}).status_code == 204
    assert row().status == DeliveryStatus.RECEIVED
    monkeypatch.undo()
    WebhookDelivery.objects.update(received_at=timezone.now() - dt.timedelta(minutes=11))

    assert tasks.sweep_webhook_deliveries() == 1
    assert row().status == DeliveryStatus.IGNORED


def test_the_sweep_prunes_old_deliveries_but_keeps_what_waits_for_the_inbox(ours: Any) -> None:
    from social_providers import tasks

    deliver("account.connected", {"account_id": ACCOUNT}, delivery="old")
    deliver("mention.received", {"account_id": ACCOUNT, "id": "m1"}, delivery="mention")
    WebhookDelivery.objects.update(received_at=timezone.now() - dt.timedelta(days=31))

    tasks.sweep_webhook_deliveries()

    assert list(WebhookDelivery.objects.values_list("delivery_id", "status")) == [
        ("mention", DeliveryStatus.KEPT)
    ]


# -----------------------------------------------------------------------------
# Ownership: another tenant's events are dropped, never kept
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("account.disconnected", {"account_id": "acc_theirs", "reconnect_required": True}),
        ("page.removed", {"account_id": "acc_theirs", "name": "Theirs"}),
        ("comment.received", {"account_id": "acc_theirs", "id": "c1", "platform_post_id": "1"}),
        ("mention.received", {"account_id": "acc_theirs", "id": "m1"}),
        ("post.published", {"post_id": "post_theirs", "status": "published"}),
        ("post.unpublished", {"post_id": "post_theirs"}),
        ("account.connected", {"account_id": "acc_theirs"}),
    ],
)
def test_a_foreign_event_is_dropped_and_its_payload_is_not_kept(
    ours: Any, event: str, data: dict[str, Any]
) -> None:
    assert deliver(event, data).status_code == 204

    delivery = row()
    assert delivery.status == DeliveryStatus.FOREIGN
    assert delivery.payload == {}


def test_an_account_from_the_other_provider_is_foreign(ours: Any) -> None:
    ours.provider_key = "zernio"
    ours.save(update_fields=["provider_key"])

    deliver("page.removed", {"account_id": ACCOUNT, "name": "Shop"})

    assert row().status == DeliveryStatus.FOREIGN
    ours.refresh_from_db()
    assert ours.status == SocialAccountStatus.ACTIVE


# -----------------------------------------------------------------------------
# Post lifecycle (SA-42)
# -----------------------------------------------------------------------------
@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> StubVendor:
    """SocialAPI serves publishing; the reconciler reads outcomes from the stub."""
    from scheduling import reconcile

    stub = StubVendor()
    monkeypatch.setattr(reconcile, "get_platform_adapter", stub.adapter)
    monkeypatch.setattr(reconcile, "provider_key", lambda _port: "socialapi")
    return stub


def _in_flight(vendor: StubVendor, workspace: Any, user: Any, account: Any) -> Any:
    journal = MemoryJournal()
    vendor.adapter().publish(
        platform="instagram",
        provider_account_id=ACCOUNT,
        payload={"platform": "instagram", "body": "Hello", "media": [], "options": {}},
        idempotency_key="k",
        journal=journal,
    )
    target = make_target(
        workspace, user, account, age_days=0, provider_post_id=journal.provider_post_id
    )
    target.state = PostTargetState.PUBLISHING
    target.provider_state = ProviderState.PUBLISH_REQUESTED
    target.provider_key = "socialapi"
    target.published_at = None
    target.save()
    target.post.status = PostStatus.PUBLISHING
    target.post.save(update_fields=["status"])
    return target


def test_post_published_settles_the_target_from_the_providers_record(
    vendor: StubVendor, paid_workspace: Any, user: Any, ours: Any
) -> None:
    target = _in_flight(vendor, paid_workspace, user, ours)
    vendor.settle(target.provider_post_id)

    deliver("post.published", {"post_id": target.provider_post_id, "status": "published"})
    deliver(
        "post.published",
        {"post_id": target.provider_post_id, "status": "published"},
        delivery="dlv_2",
    )

    target.refresh_from_db()
    assert target.state == PostTargetState.PUBLISHED
    assert target.platform_post_id == f"native-{target.provider_post_id}"
    assert target.post.__class__.objects.get(pk=target.post_id).status == PostStatus.PUBLISHED
    assert row().status == DeliveryStatus.PROCESSED
    assert row("dlv_2").status == DeliveryStatus.IGNORED, "the second says nothing new"


def test_an_event_ahead_of_the_providers_record_changes_nothing(
    vendor: StubVendor, paid_workspace: Any, user: Any, ours: Any
) -> None:
    target = _in_flight(vendor, paid_workspace, user, ours)

    deliver("post.published", {"post_id": target.provider_post_id, "status": "published"})

    target.refresh_from_db()
    assert target.state == PostTargetState.PUBLISHING, "the provider still says publishing"
    assert row().status == DeliveryStatus.IGNORED


def test_post_failed_records_the_platforms_reason(
    vendor: StubVendor, paid_workspace: Any, user: Any, ours: Any
) -> None:
    target = _in_flight(vendor, paid_workspace, user, ours)
    vendor.settle(target.provider_post_id, published=False)

    deliver("post.failed", {"post_id": target.provider_post_id, "status": "failed"})

    target.refresh_from_db()
    assert target.state == PostTargetState.FAILED
    assert "Video too long" in target.error_detail["message"]


def test_a_live_post_taken_down_stops_being_measured(
    paid_workspace: Any, user: Any, ours: Any
) -> None:
    from analytics.services import ingest

    target = make_target(paid_workspace, user, ours, age_days=1, provider_post_id="post_live")
    target.provider_key = "socialapi"
    target.save(update_fields=["provider_key"])

    deliver("post.unpublished", {"post_id": "post_live", "account_id": ACCOUNT})
    deliver("post.unpublished", {"post_id": "post_live", "account_id": ACCOUNT}, delivery="d2")

    target.refresh_from_db()
    assert target.provider_state == ProviderState.REMOVED
    assert target.state == PostTargetState.PUBLISHED, "it was published; history says so"
    assert row().status == DeliveryStatus.PROCESSED
    assert row("d2").status == DeliveryStatus.IGNORED
    assert ingest.capture_due() == 0


def test_deleting_a_draft_we_discarded_is_nothing(
    paid_workspace: Any, user: Any, ours: Any
) -> None:
    target = make_target(paid_workspace, user, ours, provider_post_id="post_draft")
    target.state = PostTargetState.FAILED
    target.provider_state = ProviderState.DISCARDED
    target.provider_key = "socialapi"
    target.save()

    deliver("post.deleted", {"post_id": "post_draft", "was_status": "draft"})

    assert row().status == DeliveryStatus.IGNORED
    target.refresh_from_db()
    assert target.provider_state == ProviderState.DISCARDED


# -----------------------------------------------------------------------------
# Account lifecycle (SA-42, plan G14)
# -----------------------------------------------------------------------------
def test_a_reconnect_prompt_parks_the_account_and_tells_its_admins(ours: Any, user: Any) -> None:
    from channels.services import AccountNotPublishableError, ensure_publishable

    deliver(
        "account.disconnected",
        {"account_id": ACCOUNT, "reason": "access_revoked", "reconnect_required": True},
    )

    ours.refresh_from_db()
    assert ours.status == SocialAccountStatus.REAUTH_REQUIRED
    assert ours.reconnect_reason == "Access to this account was revoked on the platform."
    notice = Notification.objects.get(event_key=EventKey.ACCOUNT_RECONNECT)
    assert notice.user == user
    assert "Reconnect" in notice.payload["subject"]
    with pytest.raises(AccountNotPublishableError):
        ensure_publishable(ours)


def test_a_revocation_on_the_platform_revokes_the_account(ours: Any) -> None:
    deliver("account.disconnected", {"account_id": ACCOUNT, "platform": "instagram"})

    ours.refresh_from_db()
    assert ours.status == SocialAccountStatus.REVOKED


def test_our_own_disconnect_echoed_back_tells_nobody(ours: Any) -> None:
    ours.status = SocialAccountStatus.REVOKED
    ours.save(update_fields=["status"])

    deliver("account.disconnected", {"account_id": ACCOUNT})

    assert row().status == DeliveryStatus.IGNORED
    assert not Notification.objects.exists()


def test_a_removed_page_is_named_in_the_prompt(ours: Any) -> None:
    deliver(
        "page.removed",
        {"account_id": ACCOUNT, "platform_page_id": "1029", "name": "ACME Storefront"},
    )

    ours.refresh_from_db()
    assert ours.status == SocialAccountStatus.PAGE_REMOVED
    assert "ACME Storefront" in ours.reconnect_reason


# -----------------------------------------------------------------------------
# Comments and mentions (SA-42, L-4a)
# -----------------------------------------------------------------------------
def _comment(comment_id: str = "sapi_cmt_1", platform_post_id: str = "native-9") -> dict[str, Any]:
    return {
        "id": comment_id,
        "type": "comment",
        "platform": "instagram",
        "account_id": ACCOUNT,
        "platform_post_id": platform_post_id,
        "author": {"id": "10215", "name": "Jane Smith"},
        "content": {"text": "Love this product!", "media": []},
        "received_at": "2026-10-13T14:30:00Z",
    }


@pytest.fixture
def advanced(paid_workspace: Any, plans: dict[str, Any]) -> Any:
    paid_workspace.organization.plan = plans["advanced"]
    paid_workspace.organization.save(update_fields=["plan"])
    return paid_workspace


def _published(workspace: Any, user: Any, account: Any) -> Any:
    target = make_target(workspace, user, account, provider_post_id="post_9")
    target.platform_post_id = "native-9"
    target.provider_key = "socialapi"
    target.save(update_fields=["platform_post_id", "provider_key"])
    return target


def test_on_the_webhook_plan_a_comment_is_stored_once(advanced: Any, user: Any, ours: Any) -> None:
    target = _published(advanced, user, ours)

    deliver("comment.received", _comment())
    deliver("comment.received", _comment(), delivery="dlv_retry_with_new_id")

    comment = AudienceComment.objects.get(post_target=target)
    assert (comment.external_id, comment.author, comment.body) == (
        "sapi_cmt_1",
        "Jane Smith",
        "Love this product!",
    )


def test_a_comment_by_webhook_and_by_poll_is_one_row(advanced: Any, user: Any, ours: Any) -> None:
    from analytics.providers.base import CommentSnapshot
    from analytics.services.ingest import store_comments

    target = _published(advanced, user, ours)
    deliver("comment.received", _comment())

    polled = store_comments(
        target, [CommentSnapshot(external_id="sapi_cmt_1", body="Love this product!")]
    )

    assert polled == 0
    assert AudienceComment.objects.filter(post_target=target).count() == 1


def test_below_the_top_plan_comments_wait_for_their_poll(
    paid_workspace: Any, user: Any, ours: Any
) -> None:
    _published(paid_workspace, user, ours)

    deliver("comment.received", _comment())

    assert row().status == DeliveryStatus.IGNORED
    assert not AudienceComment.objects.exists()


def test_a_comment_naming_no_post_is_left_for_the_poll(advanced: Any, ours: Any) -> None:
    deliver("comment.received", _comment(platform_post_id=""))

    assert row().status == DeliveryStatus.UNMATCHED


def test_a_comment_on_a_post_we_did_not_publish_is_ignored(advanced: Any, ours: Any) -> None:
    deliver("comment.received", _comment(platform_post_id="native-elsewhere"))

    assert row().status == DeliveryStatus.IGNORED
    assert not AudienceComment.objects.exists()


def test_a_mention_is_kept_for_the_inbox(ours: Any) -> None:
    deliver("mention.received", {"account_id": ACCOUNT, "id": "m1"})

    assert row().status == DeliveryStatus.KEPT
    assert row().payload["data"]["id"] == "m1"


# -----------------------------------------------------------------------------
# Registration (SA-41)
# -----------------------------------------------------------------------------
@pytest.fixture
def dashboard(monkeypatch: pytest.MonkeyPatch) -> ReadVendor:
    from social_providers.management.commands import socialapi_webhooks

    stub = ReadVendor()
    stub.routes["GET /webhooks"] = {"data": []}
    stub.routes["POST /webhooks"] = {
        "id": "wh_1",
        "url": "https://abc.trycloudflare.com/api/v1/webhooks/socialapi/",
        "secret": "s3cr3t-shown-once",
    }
    monkeypatch.setattr(socialapi_webhooks, "SocialAPIClient", stub.client)
    return stub


def run(*args: str) -> str:
    out = io.StringIO()
    call_command("socialapi_webhooks", *args, stdout=out)
    return out.getvalue()


def test_register_prints_the_secret_once_and_subscribes_the_routed_events(
    dashboard: ReadVendor,
) -> None:
    from social_providers.socialapi.events import HANDLERS

    output = run("register", "--url", "https://abc.trycloudflare.com/api/v1/webhooks/socialapi/")

    assert "s3cr3t-shown-once" in output
    assert "SOCIALAPI_WEBHOOK_SECRET" in output
    assert set(dashboard.bodies[0]["events"]) >= set(HANDLERS)
    assert dashboard.bodies[0]["url"].startswith("https://")
    assert not WebhookDelivery.objects.exists()


@pytest.mark.parametrize(
    "url", ["http://abc.example.com/hook/", "https://localhost:8000/api/v1/webhooks/socialapi/"]
)
def test_register_refuses_an_address_the_vendor_cannot_use(dashboard: ReadVendor, url: str) -> None:
    with pytest.raises(CommandError):
        run("register", "--url", url)
    assert dashboard.calls == []


def test_register_refuses_a_second_endpoint_for_the_same_url(dashboard: ReadVendor) -> None:
    url = "https://abc.trycloudflare.com/api/v1/webhooks/socialapi/"
    dashboard.routes["GET /webhooks"] = {"data": [{"id": "wh_0", "url": url, "is_active": True}]}

    with pytest.raises(CommandError, match="rotate"):
        run("register", "--url", url)

    assert "s3cr3t-shown-once" in run("rotate", "--url", url)


def test_retiring_the_only_active_endpoint_needs_force(dashboard: ReadVendor) -> None:
    dashboard.routes["GET /webhooks"] = {
        "data": [{"id": "wh_0", "url": "https://x.test/", "is_active": True}]
    }
    dashboard.routes["DELETE /webhooks/wh_0"] = {"deleted": True}

    with pytest.raises(CommandError, match="only active endpoint"):
        run("retire", "wh_0")

    assert "retired" in run("retire", "wh_0", "--force")


def test_status_reports_the_secret_endpoints_and_arrivals(dashboard: ReadVendor, ours: Any) -> None:
    dashboard.routes["GET /webhooks"] = {
        "data": [
            {"id": "wh_0", "url": "https://x.test/", "is_active": True, "events": ["post.failed"]}
        ]
    }
    deliver("account.connected", {"account_id": ACCOUNT})

    output = run("status")

    assert "Secret: set" in output
    assert "wh_0" in output
    assert "not subscribed:" in output
    assert "IGNORED" in output


def test_a_deployment_without_the_webhook_secret_is_warned(settings: Any) -> None:
    from django.core.checks import run_checks

    settings.SOCIALAPI_WEBHOOK_SECRET = ""
    settings.SOCIALAPI_API_KEY = "k"
    settings.SOCIAL_PROVIDER = "socialapi"

    ids = {message.id for message in run_checks(include_deployment_checks=True)}

    assert "social_providers.W002" in ids
