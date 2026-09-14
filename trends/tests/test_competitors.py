"""Competitor tracking as a trend source kind (P6-08).

BUILD-PLAN is explicit that this is **not a parallel pipeline** — it overlaps
the tracked-accounts source, so it is a source kind running the same stages and
inheriting stage 3's per-kind percentile partition. The tests below hold both
halves of that: that it really does reuse the pipeline, and that the one thing
it cannot reuse — the shared, category-wide corpus — stays separated.

The separation is the part with teeth. Every source before this one was shared
across a category, which is what makes the trend engine cheap. Who a brand
watches is competitive information about that brand, so a competitor source is
owned by one workspace and its items must not appear in anybody else's window.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.exceptions import QuotaExceeded
from content.models import Platform
from trends.models import TrendItem, TrendSource, TrendSourceKind
from trends.services import competitors, ingest

pytestmark = pytest.mark.django_db


@pytest.fixture
def rival_workspace(plans: dict[str, Any], category: Any) -> Any:
    """A second brand in the same category — the tenancy boundary under test.

    Same category on purpose: if isolation held only because two workspaces
    looked at different categories, it would not be isolation.
    """
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    user = get_user_model().objects.create_user(email="rival@example.com", password="pw")
    workspace = provision_workspace(user, name="Rival Studio")
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])
    workspace.category = category
    workspace.save(update_fields=["category"])
    return workspace


@pytest.fixture
def branded_workspace(paid_workspace: Any, category: Any) -> Any:
    paid_workspace.category = category
    paid_workspace.save(update_fields=["category"])
    return paid_workspace


# -----------------------------------------------------------------------------
# Tracking
# -----------------------------------------------------------------------------
def test_tracking_creates_a_workspace_owned_source(branded_workspace: Any) -> None:
    source = competitors.track(
        branded_workspace, platform=Platform.INSTAGRAM, handle="@Rival-Ceramics", label="Rival"
    )

    assert source.kind == TrendSourceKind.COMPETITOR
    assert source.workspace_id == branded_workspace.pk
    # Normalised: `@Rival` and `rival` are the same account, and two rows for
    # one competitor would double its weight in every comparison.
    assert source.handle == "rival-ceramics"


def test_tracking_the_same_handle_twice_is_the_same_source(branded_workspace: Any) -> None:
    first = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    second = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="@RIVAL")

    assert first.pk == second.pk
    assert competitors.tracked(branded_workspace).count() == 1


def test_a_blank_handle_is_refused(branded_workspace: Any) -> None:
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="  @ ")


def test_the_cap_comes_from_the_plan_not_a_constant(branded_workspace: Any) -> None:
    """Part 7 rule 10 — how many competitors a plan buys is an admin-editable
    row, and exhaustion is a 402 with an upgrade path."""
    plan = branded_workspace.organization.plan
    plan.max_tracked_competitors = 1
    plan.save(update_fields=["max_tracked_competitors"])

    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival-one")
    with pytest.raises(QuotaExceeded):
        competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival-two")


def test_untracking_keeps_what_was_already_measured(branded_workspace: Any) -> None:
    """Deleting the source would take its items with it, turning "we stopped
    watching them in March" into "we never watched them"."""
    source = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)
    before = TrendItem.objects.filter(source=source).count()

    competitors.untrack(source)

    source.refresh_from_db()
    assert source.is_active is False
    assert TrendItem.objects.filter(source=source).count() == before
    assert competitors.tracked(branded_workspace).count() == 0


# -----------------------------------------------------------------------------
# Tenancy — the one thing this kind cannot share
# -----------------------------------------------------------------------------
def test_a_competitor_source_never_enters_the_shared_corpus(
    branded_workspace: Any, category: Any
) -> None:
    """One brand's competitor list must not appear on every other brand's
    trend page. The filter lives in `sources_for`, not in each caller."""
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")

    shared = ingest.sources_for(category, Platform.INSTAGRAM)

    assert all(source.workspace_id is None for source in shared)


def test_one_workspace_cannot_see_anothers_tracked_list(
    branded_workspace: Any, rival_workspace: Any
) -> None:
    theirs = rival_workspace
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")

    assert competitors.tracked(theirs).count() == 0


def test_two_workspaces_may_track_the_same_competitor(
    branded_workspace: Any, rival_workspace: Any
) -> None:
    """The partial unique constraint has to allow this — two brands watching
    the same rival is the normal case, not a conflict."""
    theirs = rival_workspace
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    competitors.track(theirs, platform=Platform.INSTAGRAM, handle="rival")

    assert TrendSource.objects.filter(kind=TrendSourceKind.COMPETITOR, handle="rival").count() == 2


# -----------------------------------------------------------------------------
# The pipeline, reused
# -----------------------------------------------------------------------------
def test_refresh_ingests_scores_and_stays_idempotent(branded_workspace: Any) -> None:
    source = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")

    first = competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)
    assert first
    assert all(item.composite_score > 0 for item in first)

    competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)
    # `(source, external_id)` is the identity: a re-run updates rather than
    # inserting a second copy, exactly as the shared corpus does.
    assert TrendItem.objects.filter(source=source).count() == len(first)


def test_two_competitors_do_not_share_one_corpus(branded_workspace: Any) -> None:
    """Ingest upserts on `(source, external_id)`, so a vendor that returned the
    same ids for every account would make every competitor look like one."""
    one = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival-one")
    two = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival-two")

    competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)

    handles = set(TrendItem.objects.filter(source=one).values_list("author_handle", flat=True))
    assert handles == {"rival-one"}
    assert TrendItem.objects.filter(source=two).exists()


def test_refresh_with_nothing_tracked_does_nothing(branded_workspace: Any) -> None:
    assert competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM) == []


def test_competitor_items_are_scored_within_their_own_kind(branded_workspace: Any) -> None:
    """Stage 3's partition, inherited rather than reinvented.

    A competitor's follower count and a Reddit thread's are not comparable, and
    pooling them would let whichever has the more generous denominator win
    every ranking.
    """
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    scored = competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)

    # Percentiles within a partition span it: the best and worst of the kind
    # are at the ends, which only holds if the partition was the kind alone.
    engagements = sorted(item.engagement_score for item in scored)
    assert engagements[0] == pytest.approx(0.0)
    assert engagements[-1] == pytest.approx(1.0)


# -----------------------------------------------------------------------------
# The comparison
# -----------------------------------------------------------------------------
def test_the_comparison_computes_both_sides_the_same_way(
    branded_workspace: Any, user: Any, social_account: Any
) -> None:
    """Our impressions against their likes is the kind of chart that reads as
    insight and means nothing."""
    from analytics.models import Availability, MetricSource, PostMetric
    from content.models import PostStatus, PostTarget
    from content.services.posts import create_post

    social_account.followers_cached = 10_000
    social_account.save(update_fields=["followers_cached"])
    post = create_post(workspace=branded_workspace, author=user, master_body="Ours")
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])
    target = PostTarget.objects.create(
        post=post, platform=Platform.INSTAGRAM, social_account=social_account
    )
    PostMetric.objects.create(
        post_target=target,
        captured_at=timezone_now(),
        likes=500,
        comments=40,
        shares=10,
        saves=20,
        availability=Availability.MEASURED,
        source=MetricSource.PROVIDER,
        provider_key="zernio",
    )

    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)

    result = competitors.comparison(branded_workspace, platform=Platform.INSTAGRAM)

    assert result["us"]["engagement_rate"] is not None
    assert result["competitors"][0]["engagement_rate"] is not None
    # Both sides are interactions over audience — the numbers are comparable
    # because they are the same calculation, not because they look alike.
    assert result["us"]["followers"] == 10_000
    assert result["competitors"][0]["posts"] == 3


def test_a_competitor_with_no_audience_reported_has_a_null_rate_not_zero(
    branded_workspace: Any,
) -> None:
    """Part 7 rule 12, applied to somebody else's account: an unreported
    follower count is unmeasured, not "they get no engagement"."""
    source = competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="rival")
    competitors.refresh(branded_workspace, platform=Platform.INSTAGRAM)
    TrendItem.objects.filter(source=source).update(author_followers=0)

    result = competitors.comparison(branded_workspace, platform=Platform.INSTAGRAM)

    assert result["competitors"][0]["followers"] is None
    assert result["competitors"][0]["engagement_rate"] is None


def test_our_side_reads_only_analysable_captures(
    branded_workspace: Any, user: Any, social_account: Any
) -> None:
    """A fake row must not reach a comparison a customer acts on (Part 7 rule
    17), and an unavailable capture is excluded rather than counted as zero."""
    from analytics.models import Availability, MetricSource, PostMetric
    from content.models import PostStatus, PostTarget
    from content.services.posts import create_post

    social_account.followers_cached = 10_000
    social_account.save(update_fields=["followers_cached"])
    post = create_post(workspace=branded_workspace, author=user, master_body="Ours")
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])
    target = PostTarget.objects.create(
        post=post, platform=Platform.INSTAGRAM, social_account=social_account
    )
    PostMetric.objects.create(
        post_target=target,
        captured_at=timezone_now(),
        likes=9_000,
        availability=Availability.MEASURED,
        source=MetricSource.FAKE,
        provider_key="fake",
    )

    result = competitors.comparison(branded_workspace, platform=Platform.INSTAGRAM)

    assert result["us"]["posts"] == 0
    assert result["us"]["interactions"] == 0


def test_a_competitor_that_published_nothing_reports_zero_posts_and_a_null_rate(
    branded_workspace: Any,
) -> None:
    """We watched and they published nothing. That is a fact worth showing, and
    it is not engagement of zero."""
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="quiet-rival")

    result = competitors.comparison(branded_workspace, platform=Platform.INSTAGRAM)

    assert result["competitors"][0]["posts"] == 0
    assert result["competitors"][0]["engagement_rate"] is None
    assert result["competitors"][0]["top_post"] is None


def test_the_comparison_is_scoped_to_the_asking_workspace(
    branded_workspace: Any, rival_workspace: Any
) -> None:
    theirs = rival_workspace
    competitors.track(branded_workspace, platform=Platform.INSTAGRAM, handle="ours-to-watch")
    competitors.track(theirs, platform=Platform.INSTAGRAM, handle="theirs-to-watch")

    result = competitors.comparison(branded_workspace, platform=Platform.INSTAGRAM)

    assert [row["handle"] for row in result["competitors"]] == ["ours-to-watch"]


def timezone_now() -> Any:
    from django.utils import timezone

    return timezone.now()
