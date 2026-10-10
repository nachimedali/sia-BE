"""The delta feed and the ladder stop (P0-31, P0-40) and TikTok's silence (P0-03).

Three things that all come back to the same principle: measure what the vendor
actually said, at the cost the vendor actually charges, and never let a gap
become a number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from analytics.models import Availability, PostMetric, ProviderCursor
from analytics.providers.base import CommentSnapshot, RawMetricPayload
from analytics.services import ingest
from analytics.tests.conftest import make_target

pytestmark = pytest.mark.django_db


def _payload(post_id: str, *, platform: str = "instagram", **body: int) -> RawMetricPayload:
    return RawMetricPayload(
        provider_key="fake",
        platform=platform,
        provider_post_id=post_id,
        schema_version=1,
        body=body or {"impressions": 500, "likes": 25},
        availability="MEASURED",
        fetched_at=timezone.now(),
    )


# -----------------------------------------------------------------------------
# The delta feed — P0-31
# -----------------------------------------------------------------------------
def test_the_feed_is_not_followed_before_the_ladder_has_bootstrapped(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The feed is a rolling seven-day log. Following it first would silently
    skip every post older than the window and never come back for them, which
    is the failure mode that looks like working software."""
    target = make_target(paid_workspace, user, social_account, age_days=0)
    metrics_provider.delta_batches = [[_payload(target.provider_post_id)]]

    assert ingest.follow_delta() == 0
    assert not PostMetric.objects.exists()


def test_a_bootstrapped_feed_satisfies_a_due_rung(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        target = make_target(paid_workspace, user, social_account, age_days=0)
        ingest.mark_bootstrapped("fake")

    metrics_provider.delta_batches = [[_payload(target.provider_post_id)]]
    with time_machine.travel(start + dt.timedelta(hours=1, minutes=5), tick=False):
        assert ingest.follow_delta() == 1

    row = PostMetric.objects.get()
    # Stored at the rung's nominal time, not the wall clock the feed arrived
    # at — the feed changes the transport, not the schedule.
    assert row.captured_at == start + dt.timedelta(hours=1)
    assert row.impressions == 500


def test_a_feed_arrival_for_a_target_owed_nothing_writes_nothing(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The feed reports every movement; the ladder decides which we keep.
    Otherwise a chatty post would accrue a snapshot every ten minutes and the
    decay curve would be computed over a different grid than every other
    post's."""
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        target = make_target(paid_workspace, user, social_account, age_days=0)
        ingest.mark_bootstrapped("fake")

    with time_machine.travel(start + dt.timedelta(hours=1, minutes=5), tick=False):
        assert ingest.capture_due() == 1
        metrics_provider.delta_batches = [[_payload(target.provider_post_id)]]
        assert ingest.follow_delta() == 0

    assert PostMetric.objects.count() == 1


def test_the_cursor_advances_and_is_remembered(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    ingest.mark_bootstrapped("fake")
    metrics_provider.delta_batches = [[]]

    ingest.follow_delta()

    state = ProviderCursor.objects.get(provider_key="fake")
    assert state.empty_reads == 1


def test_an_unknown_post_in_the_feed_is_ignored(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The feed spans every account on the provider profile, including posts
    this deployment did not publish. They are not ours to record."""
    ingest.mark_bootstrapped("fake")
    metrics_provider.delta_batches = [[_payload("someone-elses-post")]]

    assert ingest.follow_delta() == 0
    assert not PostMetric.objects.exists()


# -----------------------------------------------------------------------------
# The ladder stop — P0-40 / U-4
# -----------------------------------------------------------------------------
def test_the_ladder_stops_at_thirty_days() -> None:
    """The built ladder ran weekly to the plan horizon with no stop, so an
    Advanced workspace paid a call per post per week for two years to watch
    numbers that stopped moving in month one."""
    published = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

    rungs = ingest.rungs(published, horizon_days=730)

    assert max(rungs) <= published + dt.timedelta(days=ingest.CAPTURE_STOP_DAYS)


def test_a_horizon_shorter_than_the_stop_still_wins() -> None:
    """The stop is a ceiling on asking, not a floor. A 7-day plan does not get
    30 days of captures because the stop is longer than its horizon."""
    published = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)

    rungs = ingest.rungs(published, horizon_days=7)

    assert max(rungs) <= published + dt.timedelta(days=7)


# -----------------------------------------------------------------------------
# TikTok reports unavailable, never an empty thread — P0-03
# -----------------------------------------------------------------------------
def test_tiktok_records_unavailability_rather_than_no_comments(
    paid_workspace: Any, user: Any, metrics_provider: Any
) -> None:
    """L-3: TikTok exposes no comment endpoint at any price. An empty thread
    would read as "nobody said anything", which is a claim about the audience
    we have no basis for."""
    from channels.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="tiktok",
        handle="@acme-tt",
        provider_account_id="acct-tiktok-1",
    )
    target = make_target(paid_workspace, user, account)

    assert ingest.capture_comments(target) == 0

    marker = target.post_comments.get()
    assert marker.availability == Availability.UNAVAILABLE
    assert marker.external_id == ingest.UNAVAILABLE_MARKER


def test_the_unavailability_marker_is_written_once(
    paid_workspace: Any, user: Any, metrics_provider: Any
) -> None:
    from channels.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="tiktok",
        handle="@acme-tt",
        provider_account_id="acct-tiktok-2",
    )
    target = make_target(paid_workspace, user, account)

    ingest.capture_comments(target)
    ingest.capture_comments(target)

    assert target.post_comments.count() == 1


def test_the_marker_never_counts_as_a_comment(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """P0-03's real point: the marker must be excluded from every
    comment-derived denominator, and it is excluded by the queryset rather than
    by each caller remembering to filter it."""
    from analytics.models import AudienceComment
    from channels.models import SocialAccount

    tiktok = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="tiktok",
        handle="@acme-tt",
        provider_account_id="acct-tiktok-3",
    )
    ingest.capture_comments(make_target(paid_workspace, user, tiktok))

    instagram_target = make_target(paid_workspace, user, social_account)
    metrics_provider.comments_for[instagram_target.provider_post_id] = [
        CommentSnapshot(external_id="c-1", body="love this", posted_at=timezone.now())
    ]
    ingest.capture_comments(instagram_target)

    assert AudienceComment.objects.count() == 2
    assert AudienceComment.objects.measured().count() == 1
