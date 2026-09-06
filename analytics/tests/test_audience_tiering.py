"""Audience engagement, tiered by plan (L-4a, P0-34, P0-35, P0-36).

Reading is free from the vendor, so none of this is a cost pass-through — it is
a product ladder over freshness, depth and reply. Two properties are asserted
everywhere below:

* **it degrades, it never fabricates** — a lower tier sees less, not something
  untrue;
* **unavailable is not empty** — a platform that cannot report is rendered
  differently from a post nobody engaged with.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from typing import Any

import pytest
import time_machine
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from analytics import webhooks
from analytics.models import AudienceCaptureState, AudienceComment, Availability
from analytics.providers.base import CommentSnapshot
from analytics.services import audience, ingest
from analytics.tests.conftest import make_target
from billing.models import ReactionDetail, ReplyLedger
from billing.services import ledger
from billing.services.entitlements import entitlements_for

pytestmark = pytest.mark.django_db

WEBHOOK_SECRET = "whsec-test"


# -----------------------------------------------------------------------------
# Capture cadence — P0-34
# -----------------------------------------------------------------------------
def test_the_cadence_comes_from_the_plan_not_a_constant(
    workspace: Any, plans: dict[str, Any]
) -> None:
    """Part 7 rule 10: no commercial number is hardcoded. Daily on the trial,
    six-hourly on Pro, webhook-driven on Advanced."""
    workspace.plan = plans["free"]
    workspace.save(update_fields=["plan"])
    assert entitlements_for(workspace).comment_capture_interval() == dt.timedelta(days=1)

    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    assert entitlements_for(workspace).comment_capture_interval() == dt.timedelta(hours=6)

    workspace.plan = plans["advanced"]
    workspace.save(update_fields=["plan"])
    # `None` means "subscribed", not "poll constantly": the provider caches
    # both read endpoints for ten minutes and says not to poll them.
    assert entitlements_for(workspace).comment_capture_interval() is None


def test_a_second_capture_inside_the_interval_is_skipped(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    target = make_target(paid_workspace, user, social_account)
    now = timezone.now()
    metrics_provider.comments_for[target.provider_post_id] = [
        CommentSnapshot(external_id="c-1", body="love this", posted_at=now)
    ]

    with time_machine.travel(now, tick=False):
        assert ingest.capture_comments(target) == 1
    with time_machine.travel(now + dt.timedelta(hours=1), tick=False):
        metrics_provider.comment_calls.clear()
        assert ingest.capture_comments(target) == 0
        assert metrics_provider.comment_calls == []


def test_the_interval_elapsing_permits_another_read(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    target = make_target(paid_workspace, user, social_account)
    now = timezone.now()

    with time_machine.travel(now, tick=False):
        ingest.capture_comments(target)
    with time_machine.travel(now + dt.timedelta(hours=7), tick=False):
        metrics_provider.comment_calls.clear()
        ingest.capture_comments(target)

    assert metrics_provider.comment_calls != []


def test_a_quiet_poll_still_spends_the_cadence(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """A post nobody commented on must not be re-polled every tick just
    because there was nothing to store."""
    target = make_target(paid_workspace, user, social_account)

    assert ingest.capture_comments(target) == 0

    state = AudienceCaptureState.objects.get(post_target=target)
    assert state.last_captured_at is not None


def test_the_advanced_tier_does_not_poll_at_all(
    workspace: Any, user: Any, plans: dict[str, Any], metrics_provider: Any
) -> None:
    from channels.models import SocialAccount

    workspace.plan = plans["advanced"]
    workspace.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        handle="@adv",
        provider_account_id="acct-adv-1",
    )
    target = make_target(workspace, user, account)

    assert ingest.capture_comments(target) == 0
    assert metrics_provider.comment_calls == []


# -----------------------------------------------------------------------------
# History depth — P0-34
# -----------------------------------------------------------------------------
def test_comment_history_is_bounded_by_the_plans_horizon(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """L-4a reuses `analytics_history_days` rather than inventing a second
    retention ladder — 7 / 90 / 730, the horizons already enforced."""
    target = make_target(paid_workspace, user, social_account)
    now = timezone.now()
    for age, external in ((5, "recent"), (200, "ancient")):
        AudienceComment.objects.create(
            post_target=target,
            external_id=external,
            body="hello",
            posted_at=now - dt.timedelta(days=age),
        )

    visible = audience.comments_for(paid_workspace, now=now)

    assert [c.external_id for c in visible] == ["recent"]


# -----------------------------------------------------------------------------
# Reaction depth — L-4a
# -----------------------------------------------------------------------------
def test_the_lower_tier_sees_a_real_total_not_a_fabricated_breakdown() -> None:
    view = audience.visible_reactions({"like": 10, "love": 4}, detail=ReactionDetail.TOTAL)

    assert view.available is True
    assert view.total == 14
    assert view.by_type is None


def test_the_upper_tier_sees_the_breakdown() -> None:
    view = audience.visible_reactions({"like": 10, "love": 4}, detail=ReactionDetail.PER_TYPE)

    assert view.by_type == {"like": 10, "love": 4}


def test_a_platform_that_does_not_break_reactions_down_is_unavailable_at_every_tier() -> None:
    """No plan buys data the vendor does not have. `None` in, unavailable out
    — and unavailable is not the same as a breakdown of zeros."""
    for detail in (ReactionDetail.TOTAL, ReactionDetail.PER_TYPE, ReactionDetail.REACTORS):
        view = audience.visible_reactions(None, detail=detail)
        assert view.available is False
        assert view.total is None


def test_no_reactions_is_different_from_no_breakdown() -> None:
    empty = audience.visible_reactions({}, detail=ReactionDetail.PER_TYPE)

    assert empty.available is True
    assert empty.total == 0


# -----------------------------------------------------------------------------
# Reply — P0-36
# -----------------------------------------------------------------------------
def _comment(workspace: Any, user: Any, account: Any) -> AudienceComment:
    target = make_target(workspace, user, account)
    return AudienceComment.objects.create(
        post_target=target,
        external_id="c-1",
        body="when does it ship",
        posted_at=timezone.now(),
    )


def test_reply_is_refused_below_advanced(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """402 with an upgrade, not 403: the user cannot do this *yet*, and the
    difference matters to what the UI offers them."""
    from common.exceptions import FeatureNotAvailable

    comment = _comment(paid_workspace, user, social_account)

    with pytest.raises(FeatureNotAvailable):
        audience.reply_to(comment, body="next week!", actor=user)

    assert metrics_provider.replies == []


def test_advanced_can_reply_and_the_spend_is_recorded(
    workspace: Any, user: Any, plans: dict[str, Any], metrics_provider: Any, organization: Any
) -> None:
    from channels.models import SocialAccount

    workspace.plan = plans["advanced"]
    workspace.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram", handle="@a", provider_account_id="acct-r-1"
    )
    comment = _comment(workspace, user, account)

    external_id = audience.reply_to(comment, body="next week!", actor=user)

    assert external_id.startswith("fake-reply-")
    assert metrics_provider.replies == [("c-1", "next week!")]
    assert ReplyLedger.objects.count() == 1


def test_the_allowance_is_pooled_across_the_organization(
    workspace: Any, user: Any, plans: dict[str, Any], organization: Any
) -> None:
    """One workspace must not be able to spend another's headroom, which it
    cannot, because there is one pool and one counter (L-4a)."""
    from common.exceptions import InsufficientCredits

    workspace.plan = plans["advanced"]
    workspace.save(update_fields=["plan"])

    ledger.debit_reply(workspace, actor=user, allowance=2)
    ledger.debit_reply(workspace, actor=user, allowance=2)

    with pytest.raises(InsufficientCredits):
        ledger.debit_reply(workspace, actor=user, allowance=2)

    assert ledger.replies_used_this_month(organization) == 2


def test_the_allowance_resets_with_the_calendar_month(
    workspace: Any, user: Any, organization: Any
) -> None:
    with time_machine.travel(dt.datetime(2026, 6, 20, tzinfo=dt.UTC), tick=False):
        ledger.debit_reply(workspace, actor=user, allowance=1)
        assert ledger.replies_used_this_month(organization) == 1

    with time_machine.travel(dt.datetime(2026, 7, 1, tzinfo=dt.UTC), tick=False):
        assert ledger.replies_used_this_month(organization) == 0


def test_the_reply_ledger_is_append_only(workspace: Any, user: Any, organization: Any) -> None:
    from common.records import AppendOnlyError

    entry = ledger.debit_reply(workspace, actor=user)
    entry.note = "edited"

    with pytest.raises(AppendOnlyError):
        entry.save()


# -----------------------------------------------------------------------------
# The webhook — P0-35
# -----------------------------------------------------------------------------
def _signed(body: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()


@override_settings(ZERNIO_WEBHOOK_SECRET=WEBHOOK_SECRET)
def test_a_signed_delivery_stores_the_comment(
    client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    target = make_target(paid_workspace, user, social_account)
    raw, signature = _signed(
        {"commentId": "wh-1", "postId": target.provider_post_id, "text": "love this"}
    )

    response = client.post(
        reverse("webhook-zernio-comment"),
        data=raw,
        content_type="application/json",
        HTTP_X_ZERNIO_SIGNATURE=signature,
    )

    assert response.status_code == 200
    assert AudienceComment.objects.get(external_id="wh-1").availability == Availability.MEASURED


@override_settings(ZERNIO_WEBHOOK_SECRET=WEBHOOK_SECRET)
def test_an_unsigned_delivery_is_refused(client: Any) -> None:
    raw, _ = _signed({"commentId": "wh-1", "postId": "zp-1"})

    response = client.post(
        reverse("webhook-zernio-comment"),
        data=raw,
        content_type="application/json",
        HTTP_X_ZERNIO_SIGNATURE="not-the-signature",
    )

    assert response.status_code == 400


def test_no_configured_secret_refuses_everything() -> None:
    """An unconfigured webhook that accepted anything would be an open write
    path into the audience tables."""
    with pytest.raises(webhooks.WebhookSignatureError):
        webhooks.verify(b"{}", "anything")


@override_settings(ZERNIO_WEBHOOK_SECRET=WEBHOOK_SECRET)
def test_redelivery_does_not_duplicate(paid_workspace: Any, user: Any, social_account: Any) -> None:
    """Every provider redelivers. A webhook that is not idempotent duplicates
    on every one of them."""
    target = make_target(paid_workspace, user, social_account)
    event = {"commentId": "wh-1", "postId": target.provider_post_id, "text": "hi"}

    assert webhooks.ingest_comment_event(event) is True
    assert webhooks.ingest_comment_event(event) is False
    assert AudienceComment.objects.count() == 1


@override_settings(ZERNIO_WEBHOOK_SECRET=WEBHOOK_SECRET)
def test_a_comment_for_an_unknown_post_is_dropped_not_orphaned() -> None:
    """The provider profile can carry accounts we did not publish through.
    Their audiences are not ours to record."""
    assert webhooks.ingest_comment_event({"commentId": "wh-9", "postId": "not-ours"}) is False
    assert not AudienceComment.objects.exists()
