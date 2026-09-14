"""Aggregation and the horizon refusal (P6-02, P6-03, P6-09, gate P6-G1).

**A report refuses a window it cannot honestly cover.** `analytics_history_days`
is already enforced on capture; the danger here is different and worse — a
client-facing PDF whose range was quietly clipped looks complete and is wrong,
and nobody can tell by reading it. An error is recoverable; a plausible
document is not.

Everything below reads through `analysable()`. An `UNAVAILABLE` row records
that we asked and got no answer; letting one into a denominator would drag
every average toward zero and call it a measurement.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import Availability, MetricSource, PostMetric
from content.models import Platform, PostStatus
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


def _target(workspace: Any, user: Any, platform: str = Platform.INSTAGRAM) -> Any:
    from content.models import PostTarget

    post = create_post(workspace=workspace, author=user, master_body="Hello")
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])
    return PostTarget.objects.create(post=post, platform=platform)


def _metric(target: Any, *, when: Any = None, **values: Any) -> PostMetric:
    defaults = {
        "impressions": 100,
        "likes": 10,
        "comments": 2,
        "shares": 1,
        "clicks": 5,
        "saves": 3,
        "availability": Availability.MEASURED,
        "source": MetricSource.PROVIDER,
        "provider_key": "zernio",
    }
    return PostMetric.objects.create(
        post_target=target, captured_at=when or timezone.now(), **{**defaults, **values}
    )


class TestTheHorizon:
    """P6-G1 — the ship gate."""

    def test_a_window_inside_the_horizon_is_allowed(self, workspace: Any) -> None:
        from analytics.services.reporting import ensure_within_horizon

        ends = timezone.now()
        ensure_within_horizon(workspace, starts_at=ends - dt.timedelta(days=3), ends_at=ends)

    def test_a_window_beyond_the_horizon_is_refused(self, workspace: Any) -> None:
        """Refused, **not truncated**. A PDF a client reads as "the last year"
        that silently covers seven days is worse than an error, because
        nothing in the document says so."""
        from analytics.services.reporting import ensure_within_horizon
        from common.exceptions import PaymentRequired

        ends = timezone.now()
        with pytest.raises(PaymentRequired):
            ensure_within_horizon(workspace, starts_at=ends - dt.timedelta(days=400), ends_at=ends)

    def test_the_refusal_is_a_402_with_an_upgrade(self, workspace: Any) -> None:
        # It is an entitlement the workspace can buy past, not a malformed
        # request — the same reading `require_scheduling_horizon` already takes.
        from analytics.services.reporting import ensure_within_horizon
        from common.exceptions import PaymentRequired

        ends = timezone.now()
        with pytest.raises(PaymentRequired) as raised:
            ensure_within_horizon(workspace, starts_at=ends - dt.timedelta(days=400), ends_at=ends)

        assert raised.value.status_code == 402

    def test_the_refusal_says_how_far_back_this_plan_reaches(self, workspace: Any) -> None:
        # "Too far" with no number leaves the user guessing at a value only we
        # know.
        from analytics.services.reporting import ensure_within_horizon
        from common.exceptions import PaymentRequired

        ends = timezone.now()
        with pytest.raises(PaymentRequired) as raised:
            ensure_within_horizon(workspace, starts_at=ends - dt.timedelta(days=400), ends_at=ends)

        assert "7" in str(raised.value.detail)

    def test_a_backwards_window_is_refused(self, workspace: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from analytics.services.reporting import ensure_within_horizon

        now = timezone.now()
        with pytest.raises(ValidationError):
            ensure_within_horizon(workspace, starts_at=now, ends_at=now - dt.timedelta(days=1))

    def test_an_unlimited_horizon_allows_anything(
        self, workspace: Any, plans: dict[str, Any]
    ) -> None:
        from analytics.services.reporting import ensure_within_horizon

        workspace.organization.plan = plans["advanced"]
        workspace.organization.save(update_fields=["plan"])

        ends = timezone.now()
        ensure_within_horizon(workspace, starts_at=ends - dt.timedelta(days=700), ends_at=ends)


class TestCrossChannelAggregation:
    """P6-02 — over the captures that already exist."""

    def test_it_totals_across_platforms(self, workspace: Any, user: Any) -> None:
        from analytics.services.reporting import channel_totals

        _metric(_target(workspace, user, Platform.INSTAGRAM), impressions=100, likes=10)
        _metric(_target(workspace, user, Platform.LINKEDIN), impressions=50, likes=4)

        totals = channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now())

        by_platform = {row["platform"]: row for row in totals}
        assert by_platform["instagram"]["impressions"] == 100
        assert by_platform["linkedin"]["likes"] == 4

    def test_an_unavailable_row_never_reaches_a_denominator(
        self, workspace: Any, user: Any
    ) -> None:
        """C-07 and Part 7 rule 12. A platform that reports nothing would
        otherwise drag every average it appears in toward zero and present
        that as a measurement."""
        from analytics.services.reporting import channel_totals

        target = _target(workspace, user)
        _metric(target, impressions=100)
        _metric(
            target,
            impressions=None,
            likes=None,
            availability=Availability.UNAVAILABLE,
            when=timezone.now(),
        )

        totals = channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now())

        assert totals[0]["posts"] == 1

    def test_a_fake_adapter_row_never_reaches_a_report(self, workspace: Any, user: Any) -> None:
        """Part 7 rule 17 — no fabricated row reaches a user as insight."""
        from analytics.services.reporting import channel_totals

        _metric(_target(workspace, user), source=MetricSource.FAKE, provider_key="fake")

        assert channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now()) == []

    def test_a_capture_outside_the_window_is_excluded(self, workspace: Any, user: Any) -> None:
        from analytics.services.reporting import channel_totals

        _metric(_target(workspace, user), when=_ago(30))

        assert channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now()) == []

    def test_only_the_latest_capture_per_target_is_counted(self, workspace: Any, user: Any) -> None:
        """Snapshots are cumulative, not incremental — a post captured at T+1h
        and again at T+24h has two rows describing the same post. Summing both
        would double-count every number in the report."""
        from analytics.services.reporting import channel_totals

        target = _target(workspace, user)
        _metric(target, impressions=100, when=_ago(2))
        _metric(target, impressions=180, when=_ago(1))

        totals = channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now())

        assert totals[0]["impressions"] == 180
        assert totals[0]["posts"] == 1

    def test_another_workspaces_metrics_are_not_counted(
        self, workspace: Any, user: Any, other_user: Any
    ) -> None:
        from analytics.services.reporting import channel_totals
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        _metric(_target(theirs, other_user))

        assert channel_totals(workspace, starts_at=_ago(7), ends_at=timezone.now()) == []


class TestCampaignAggregation:
    """P6-03 — over `CampaignItem`."""

    def test_it_totals_a_campaign_from_its_items(self, workspace: Any, user: Any) -> None:
        from analytics.services.reporting import campaign_totals
        from planning.models import Campaign, CampaignItem

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring",
            starts_at=_ago(10),
            ends_at=timezone.now() + dt.timedelta(days=1),
        )
        target = _target(workspace, user)
        CampaignItem.objects.create(campaign=campaign, post=target.post)
        _metric(target, impressions=250)

        totals = campaign_totals(campaign)

        assert totals["impressions"] == 250
        assert totals["posts"] == 1

    def test_a_post_outside_the_campaign_is_not_counted(self, workspace: Any, user: Any) -> None:
        from analytics.services.reporting import campaign_totals
        from planning.models import Campaign

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring",
            starts_at=_ago(10),
            ends_at=timezone.now() + dt.timedelta(days=1),
        )
        _metric(_target(workspace, user), impressions=250)

        assert campaign_totals(campaign)["posts"] == 0

    def test_an_empty_campaign_reports_zero_posts_not_a_crash(self, workspace: Any) -> None:
        from analytics.services.reporting import campaign_totals
        from planning.models import Campaign

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Empty",
            starts_at=_ago(10),
            ends_at=timezone.now() + dt.timedelta(days=1),
        )

        totals = campaign_totals(campaign)

        assert totals["posts"] == 0
        # **Null, not zero**, for the averages: no posts means no engagement
        # rate to report, and 0.0 would be a measurement we did not take.
        assert totals["engagement_rate"] is None

    def test_goals_are_compared_against_what_was_measured(self, workspace: Any, user: Any) -> None:
        from analytics.services.reporting import campaign_totals
        from planning.models import Campaign, CampaignItem

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring",
            starts_at=_ago(10),
            ends_at=timezone.now() + dt.timedelta(days=1),
            goals=[{"metric": "impressions", "target": 200}],
        )
        target = _target(workspace, user)
        CampaignItem.objects.create(campaign=campaign, post=target.post)
        _metric(target, impressions=250)

        goals = campaign_totals(campaign)["goals"]

        assert goals[0]["metric"] == "impressions"
        assert goals[0]["actual"] == 250
        assert goals[0]["met"] is True

    def test_a_goal_on_an_unmeasured_metric_reports_unavailable_not_failure(
        self, workspace: Any, user: Any
    ) -> None:
        """**Unavailable is not failure.** A goal we could not measure has not
        been missed — reporting it as missed would be a fabricated verdict
        against the customer."""
        from analytics.services.reporting import campaign_totals
        from planning.models import Campaign, CampaignItem

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring",
            starts_at=_ago(10),
            ends_at=timezone.now() + dt.timedelta(days=1),
            goals=[{"metric": "reach", "target": 500}],
        )
        target = _target(workspace, user)
        CampaignItem.objects.create(campaign=campaign, post=target.post)
        _metric(target)

        goals = campaign_totals(campaign)["goals"]

        assert goals[0]["actual"] is None
        assert goals[0]["met"] is None


def _ago(days: int) -> Any:
    return timezone.now() - dt.timedelta(days=days)


class TestTheAudienceSection:
    """P6-01 inside a report.

    Demographics are captured daily, so the danger here is not wrongness but
    volume: a thirty-day window holds thirty near-identical age breakdowns per
    account, and a section restating the same thing thirty times is one nobody
    reads.
    """

    def _capture(self, account: Any, *, when: Any, **fields: Any) -> Any:
        from analytics.models import AudienceDemographic, DemographicDimension

        defaults = {
            "dimension": DemographicDimension.AGE,
            "breakdown": {"25-34": 0.6, "35-44": 0.4},
            "availability": Availability.MEASURED,
            "provider_key": "zernio",
        }
        return AudienceDemographic.objects.create(
            social_account=account, captured_at=when, **{**defaults, **fields}
        )

    def test_only_the_newest_capture_per_dimension_reaches_the_report(
        self, paid_workspace: Any, social_account: Any
    ) -> None:
        from analytics.services.reporting import SECTION_KINDS

        now = timezone.now()
        self._capture(social_account, when=now - dt.timedelta(days=3))
        self._capture(social_account, when=now - dt.timedelta(days=1), breakdown={"25-34": 0.9})

        section = SECTION_KINDS["audience"](
            paid_workspace, starts_at=now - dt.timedelta(days=30), ends_at=now
        )

        assert len(section["demographics"]) == 1
        assert section["demographics"][0]["breakdown"] == {"25-34": 0.9}

    def test_an_unavailable_row_carries_no_breakdown_at_all(
        self, paid_workspace: Any, social_account: Any
    ) -> None:
        """Not an empty object and not zeros — the surface renders `None` as
        "we cannot see this yet"."""
        from analytics.services.reporting import SECTION_KINDS

        now = timezone.now()
        self._capture(
            social_account,
            when=now - dt.timedelta(days=1),
            breakdown=None,
            availability=Availability.UNAVAILABLE,
        )

        section = SECTION_KINDS["audience"](
            paid_workspace, starts_at=now - dt.timedelta(days=30), ends_at=now
        )

        assert section["demographics"][0]["breakdown"] is None
        assert section["demographics"][0]["availability"] == Availability.UNAVAILABLE

    def test_each_dimension_is_its_own_row(self, paid_workspace: Any, social_account: Any) -> None:
        from analytics.models import DemographicDimension
        from analytics.services.reporting import SECTION_KINDS

        now = timezone.now()
        self._capture(social_account, when=now - dt.timedelta(days=1))
        self._capture(
            social_account,
            when=now - dt.timedelta(days=1),
            dimension=DemographicDimension.COUNTRY,
            breakdown={"PT": 1.0},
        )

        section = SECTION_KINDS["audience"](
            paid_workspace, starts_at=now - dt.timedelta(days=30), ends_at=now
        )

        assert {row["dimension"] for row in section["demographics"]} == {"AGE", "COUNTRY"}

    def test_another_workspaces_audience_is_never_in_this_report(
        self, paid_workspace: Any, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        from analytics.services.reporting import SECTION_KINDS

        _theirs, _user, their_account = other_workspace
        self._capture(their_account, when=timezone.now() - dt.timedelta(days=1))

        section = SECTION_KINDS["audience"](
            paid_workspace,
            starts_at=timezone.now() - dt.timedelta(days=30),
            ends_at=timezone.now(),
        )

        assert section["demographics"] == []
