"""Metric and comment ingestion (design.md §8.9; implementation.md Phase 11).

Two of the six named tests live here: `test_metrics_polled_on_schedule` and
`test_metric_rows_immutable_and_unique_per_capture`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from analytics.models import AccountSnapshot, Comment, PostMetric, Sentiment
from analytics.services import ingest
from analytics.tests.conftest import make_target
from channels.adapters.base import CommentSnapshot, MetricSnapshot
from common.records import AppendOnlyError

pytestmark = pytest.mark.django_db

PRO_HORIZON = 90


# -----------------------------------------------------------------------------
# test_metrics_polled_on_schedule
# -----------------------------------------------------------------------------
def test_metrics_polled_on_schedule(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """The full ladder — T+1h, T+6h, T+24h, T+72h, T+7d, then weekly — driven
    with time-machine rather than sleeps (implementation.md §5)."""
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        target = make_target(paid_workspace, user, social_account, age_days=0)

    expected = [1, 6, 24, 72, 24 * 7, 24 * 14]
    for hours in expected:
        with time_machine.travel(start + dt.timedelta(hours=hours) + dt.timedelta(minutes=1)):
            assert ingest.capture_due() == 1

    captured = list(target.metrics.order_by("captured_at").values_list("captured_at", flat=True))
    assert captured == [start + dt.timedelta(hours=h) for h in expected]


def test_a_rung_is_taken_once_however_often_the_scan_runs(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Beat ticks hourly but the ladder is sparse after the first week — the
    scan has to be a no-op between rungs, not a capture per tick."""
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        target = make_target(paid_workspace, user, social_account, age_days=0)

    with time_machine.travel(start + dt.timedelta(hours=2), tick=False):
        assert ingest.capture_due() == 1
        assert ingest.capture_due() == 0
        assert ingest.capture_due() == 0

    assert target.metrics.count() == 1


def test_a_missed_window_backfills_the_oldest_rung_first(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """A worker down for a day must not skip to the newest rung: the decay
    curve the evergreen classification reads would acquire a hole."""
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        target = make_target(paid_workspace, user, social_account, age_days=0)

    with time_machine.travel(start + dt.timedelta(days=2), tick=False):
        ingest.capture_due()
        ingest.capture_due()

    taken = list(target.metrics.order_by("captured_at").values_list("captured_at", flat=True))
    assert taken == [start + dt.timedelta(hours=1), start + dt.timedelta(hours=6)]


def test_the_ladder_stops_at_the_plans_history_horizon(
    workspace: Any, user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    """Free gets 7 days of history (§4.1), so a Free workspace's ladder has no
    rung beyond it — the weekly tail belongs to plans that keep the history."""
    published = timezone.now() - dt.timedelta(days=30)

    free_rungs = ingest.rungs(published, horizon_days=7)
    pro_rungs = ingest.rungs(published, horizon_days=PRO_HORIZON)

    assert max(free_rungs) <= published + dt.timedelta(days=7)
    assert len(pro_rungs) > len(free_rungs)


def test_engagement_rate_falls_back_to_followers_without_impressions() -> None:
    """§8.9: "÷ impressions (or ÷ followers where impressions are
    unavailable)". LinkedIn personal accounts report no impressions (V1), so
    this is the normal case there, not an edge case."""
    snapshot = MetricSnapshot(impressions=0, likes=10, comments=2, shares=1, saves=1)

    rate = ingest.engagement_rate(snapshot, followers=1000)

    # (10*1 + 2*3 + 1*5 + 1*6) / 1000
    assert rate == pytest.approx(0.027)


def test_engagement_rate_is_zero_when_nothing_can_be_divided_by() -> None:
    """An honest "we do not know" rather than a number that would be ranked
    against posts where we do."""
    snapshot = MetricSnapshot(likes=10)

    assert ingest.engagement_rate(snapshot, followers=0) == 0.0


def test_a_provider_failure_leaves_the_rung_for_the_next_tick(
    published_target: Any, platform_adapter: Any, monkeypatch: Any
) -> None:
    from channels.adapters.base import PlatformError

    def explode(**_kwargs: Any) -> Any:
        raise PlatformError("provider down", retryable=True)

    monkeypatch.setattr(platform_adapter, "fetch_metrics", explode)

    assert ingest.capture_due() == 0
    assert not PostMetric.objects.exists()


def test_a_target_with_no_provider_id_is_skipped(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """A reminder-delivered post has a target row but never went through a
    provider, so there is nothing to poll."""
    target = make_target(paid_workspace, user, social_account)
    target.provider_post_id = ""
    target.save(update_fields=["provider_post_id"])

    assert ingest.capture_due() == 0


# -----------------------------------------------------------------------------
# test_metric_rows_immutable_and_unique_per_capture
# -----------------------------------------------------------------------------
def test_metric_rows_immutable_and_unique_per_capture(
    published_target: Any, platform_adapter: Any
) -> None:
    rung = timezone.now().replace(microsecond=0)
    metric = ingest.capture_target(published_target, rung)
    assert metric is not None

    # Immutable: a capture is a statement about a moment.
    metric.likes = 9999
    with pytest.raises(AppendOnlyError):
        metric.save()
    with pytest.raises(AppendOnlyError):
        metric.delete()

    # Unique per capture: the same rung twice is one row, not two.
    assert ingest.capture_target(published_target, rung) is None
    assert PostMetric.objects.filter(post_target=published_target).count() == 1


def test_comments_are_immutable_too(published_target: Any) -> None:
    comment = Comment.objects.create(
        post_target=published_target,
        external_id="c-1",
        body="love this",
        posted_at=timezone.now(),
    )

    comment.body = "edited"
    with pytest.raises(AppendOnlyError):
        comment.save()


# -----------------------------------------------------------------------------
# Comments and sentiment
# -----------------------------------------------------------------------------
def test_comments_are_ingested_and_classified(
    published_target: Any, platform_adapter: Any
) -> None:
    now = timezone.now()
    platform_adapter.comments_for[published_target.provider_post_id] = [
        CommentSnapshot(external_id="c-1", body="love this", author="@a", posted_at=now),
        CommentSnapshot(external_id="c-2", body="terrible", author="@b", posted_at=now),
        CommentSnapshot(external_id="c-3", body="when does it ship", author="@c", posted_at=now),
    ]

    assert ingest.capture_comments(published_target) == 3

    by_id = {c.external_id: c for c in Comment.objects.all()}
    assert by_id["c-1"].sentiment == Sentiment.POSITIVE
    assert by_id["c-2"].sentiment == Sentiment.NEGATIVE
    assert by_id["c-3"].sentiment == Sentiment.NEUTRAL


def test_comment_ingestion_is_idempotent_on_external_id(
    published_target: Any, platform_adapter: Any
) -> None:
    now = timezone.now()
    platform_adapter.comments_for[published_target.provider_post_id] = [
        CommentSnapshot(external_id="c-1", body="love this", posted_at=now)
    ]

    assert ingest.capture_comments(published_target) == 1
    assert ingest.capture_comments(published_target) == 0
    assert Comment.objects.count() == 1


def test_sentiment_short_circuits_the_provider_for_obvious_comments(
    published_target: Any, platform_adapter: Any
) -> None:
    """Most comments are three words long; a model call to classify "love this"
    is waste. The lexicon handles those and only the rest reach the provider."""
    from ai.providers.fake import _fake_text_provider

    _fake_text_provider.clear()
    now = timezone.now()
    platform_adapter.comments_for[published_target.provider_post_id] = [
        CommentSnapshot(external_id=f"c-{i}", body="love this", posted_at=now) for i in range(5)
    ]

    ingest.capture_comments(published_target)

    assert _fake_text_provider.calls == []


# -----------------------------------------------------------------------------
# Account snapshots
# -----------------------------------------------------------------------------
def test_account_snapshots_record_growth_and_refresh_the_cached_count(
    social_account: Any, platform_adapter: Any
) -> None:
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        assert ingest.snapshot_accounts() == 1
    with time_machine.travel(start + dt.timedelta(days=1), tick=False):
        assert ingest.snapshot_accounts() == 1

    social_account.refresh_from_db()
    assert AccountSnapshot.objects.count() == 2
    assert social_account.followers_cached == 1025


def test_a_second_snapshot_in_the_same_hour_is_not_a_second_row(
    social_account: Any, platform_adapter: Any
) -> None:
    with time_machine.travel(dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC), tick=False):
        assert ingest.snapshot_accounts() == 1
        assert ingest.snapshot_accounts() == 0

    assert AccountSnapshot.objects.count() == 1


def test_a_comment_fetch_failure_does_not_lose_the_metric_capture(
    published_target: Any, platform_adapter: Any, monkeypatch: Any
) -> None:
    """Comments are the softer half of a capture: losing them costs an
    aggregate, while losing the metric costs a moment that cannot be
    re-measured."""
    from channels.adapters.base import PlatformError

    def explode(**_kwargs: Any) -> Any:
        raise PlatformError("comments unavailable")

    monkeypatch.setattr(platform_adapter, "fetch_comments", explode)

    assert ingest.capture_comments(published_target) == 0
    assert ingest.capture_due() == 1
    assert PostMetric.objects.count() == 1


def test_a_target_with_no_provider_id_fetches_no_comments(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    target = make_target(paid_workspace, user, social_account)
    target.provider_post_id = ""
    target.save(update_fields=["provider_post_id"])

    assert ingest.capture_comments(target) == 0


def test_a_failing_account_snapshot_does_not_stop_the_others(
    social_account: Any, platform_adapter: Any, monkeypatch: Any
) -> None:
    from channels.adapters.base import PlatformError
    from channels.models import SocialAccount

    healthy = SocialAccount.objects.create(
        workspace=social_account.workspace,
        platform="threads",
        handle="@acme-th",
        provider_account_id="acct-threads-1",
    )
    original = platform_adapter.fetch_account_stats

    def sometimes(**kwargs: Any) -> Any:
        if kwargs["provider_account_id"] == social_account.provider_account_id:
            raise PlatformError("account unavailable")
        return original(**kwargs)

    monkeypatch.setattr(platform_adapter, "fetch_account_stats", sometimes)

    assert ingest.snapshot_accounts() == 1
    healthy.refresh_from_db()
    assert healthy.followers_cached > 0
