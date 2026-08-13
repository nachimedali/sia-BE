"""Derived signals (design.md §8.9).

Two of the six named tests live here: `test_percentile_computed_against_own_
history_not_global` and `test_analytics_history_bounded_by_plan_horizon`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.services import signals
from analytics.tests.conftest import capture, make_target
from content.models import PostSource

pytestmark = pytest.mark.django_db

PRO_HORIZON = 90
FREE_HORIZON = 7


# -----------------------------------------------------------------------------
# test_percentile_computed_against_own_history_not_global
# -----------------------------------------------------------------------------
def test_percentile_computed_against_own_history_not_global(
    paid_workspace: Any, user: Any, social_account: Any, other_workspace: tuple[Any, Any, Any]
) -> None:
    """§8.9: "cross-account comparison at different follower scales is
    meaningless". A workspace whose numbers are small in absolute terms must
    still see its own best post at the top of its own distribution.
    """
    # This workspace's posts all have tiny engagement rates...
    for rate in (0.001, 0.002, 0.003, 0.004, 0.010):
        capture(make_target(paid_workspace, user, social_account), rate=rate)

    # ...while another workspace's are an order of magnitude larger.
    other_ws, other_user, other_account = other_workspace
    for rate in (0.5, 0.6, 0.7, 0.8, 0.9):
        capture(make_target(other_ws, other_user, other_account), rate=rate)

    rows = signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)

    # Ranked within this workspace: its 0.010 post is the best it has, so it is
    # at the top — not buried because another account is bigger.
    assert rows[0].engagement_rate == 0.010
    assert rows[0].percentile == 100.0
    assert rows[-1].percentile == 0.0


def test_a_percentile_needs_enough_history_to_mean_anything(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """Three posts is not a distribution. "Top 100%" off two data points reads
    as authoritative and is not, so the answer is `None` until there is enough
    to rank against."""
    for rate in (0.01, 0.02):
        capture(make_target(paid_workspace, user, social_account), rate=rate)

    rows = signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)

    assert rows
    assert all(row.percentile is None for row in rows)


def test_percentiles_are_computed_per_platform(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """An Instagram engagement rate and a LinkedIn one are not the same
    measurement, so they are not ranked against each other."""
    from channels.models import SocialAccount

    linkedin = SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="linkedin",
        handle="@acme-li",
        provider_account_id="acct-li-1",
    )
    for rate in (0.001, 0.002, 0.003, 0.004, 0.005):
        capture(make_target(paid_workspace, user, social_account), rate=rate)
    for rate in (0.10, 0.20, 0.30, 0.40, 0.50):
        capture(make_target(paid_workspace, user, linkedin), rate=rate)

    rows = signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)

    instagram_best = max(
        (r for r in rows if r.platform == "instagram"), key=lambda r: r.engagement_rate
    )
    # Top of its own platform, despite every LinkedIn post beating it outright.
    assert instagram_best.percentile == 100.0


# -----------------------------------------------------------------------------
# test_analytics_history_bounded_by_plan_horizon
# -----------------------------------------------------------------------------
def test_analytics_history_bounded_by_plan_horizon(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§4.1: Free 7 days, Pro 90, Advanced 730. The bound is applied in the
    service so every caller — overview, best-times, repurpose, prompt
    grounding — respects the same one."""
    capture(make_target(paid_workspace, user, social_account, age_days=3), rate=0.05)
    capture(make_target(paid_workspace, user, social_account, age_days=40), rate=0.09)

    assert len(signals.performance(paid_workspace.pk, horizon_days=FREE_HORIZON)) == 1
    assert len(signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)) == 2


def test_a_plan_with_no_history_sees_nothing(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    capture(make_target(paid_workspace, user, social_account, age_days=1), rate=0.05)

    assert signals.performance(paid_workspace.pk, horizon_days=0) == []


# -----------------------------------------------------------------------------
# Decay, best times, attribution
# -----------------------------------------------------------------------------
def test_the_decay_ratio_separates_evergreen_from_spike(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§8.9's `evergreen_factor`: "posts that kept earning after 72h outrank
    spike-and-die posts"."""
    published = timezone.now() - dt.timedelta(days=30)
    evergreen = make_target(paid_workspace, user, social_account, age_days=30)
    spike = make_target(paid_workspace, user, social_account, age_days=30)

    for target, late_rate in ((evergreen, 0.20), (spike, 0.101)):
        capture(target, rate=0.10, at=published + dt.timedelta(hours=72))
        capture(target, rate=late_rate, at=published + dt.timedelta(days=14))

    measured = signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)
    rows = {row.target_id: row for row in measured}

    assert rows[evergreen.pk].decay_ratio == pytest.approx(2.0)
    assert rows[spike.pk].decay_ratio == pytest.approx(1.01)


def test_a_post_with_one_capture_has_a_neutral_decay_ratio(
    published_target: Any
) -> None:
    capture(published_target, rate=0.1)

    rows = signals.performance(
        published_target.post.workspace_id, horizon_days=PRO_HORIZON
    )

    assert rows[0].decay_ratio == 1.0


def test_best_times_drops_single_observation_buckets(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """One post on a Tuesday is a coincidence, not a best time."""
    monday = timezone.now().replace(hour=9) - dt.timedelta(days=timezone.now().weekday(), weeks=1)
    for _ in range(2):
        target = make_target(paid_workspace, user, social_account)
        target.published_at = monday
        target.save(update_fields=["published_at"])
        capture(target, rate=0.2)

    lonely = make_target(paid_workspace, user, social_account)
    lonely.published_at = monday + dt.timedelta(days=1)
    lonely.save(update_fields=["published_at"])
    capture(lonely, rate=0.9)

    buckets = signals.best_times(
        signals.performance(paid_workspace.pk, horizon_days=PRO_HORIZON)
    )

    assert len(buckets) == 1
    assert buckets[0]["samples"] == 2
    assert buckets[0]["weekday"] == monday.weekday()


def test_attribution_splits_ai_from_manual(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§8.9's honest self-audit — reported whichever way it comes out."""
    capture(
        make_target(paid_workspace, user, social_account, source=PostSource.AI), rate=0.30
    )
    capture(
        make_target(paid_workspace, user, social_account, source=PostSource.MANUAL), rate=0.10
    )

    result = signals.overview(paid_workspace.pk, horizon_days=PRO_HORIZON)

    assert result.by_source["AI"]["engagement_rate"] == pytest.approx(0.30)
    assert result.by_source["MANUAL"]["engagement_rate"] == pytest.approx(0.10)
    assert result.posts == 2


def test_overview_of_an_empty_workspace_is_not_an_error(paid_workspace: Any) -> None:
    result = signals.overview(paid_workspace.pk, horizon_days=PRO_HORIZON)

    assert result.posts == 0
    assert result.top == []
