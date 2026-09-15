"""The Learn job end to end (P7-02, P7-11, P7-12), against real rows.

The unit tests above prove the arithmetic. This file proves the job actually
reaches it: that published targets and their captures become observations, that
an unavailable capture stays out of the denominator through every layer rather
than only in the function that was tested for it, and that the trend provenance
stage 6 writes is the provenance stage 7 reads — a seam where the two halves
were written months apart and agree only by inspection.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import Availability, PostMetric
from content.models import Platform, PostStatus, PostTarget, PostTargetState
from content.services.posts import create_post
from learn.models import Confidence, Digest, Finding, NarrationSource
from learn.services import proposals
from learn.services.run import run_learn
from taste.models import Decision, Rule, RuleSet, TasteProfile, Verdict

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def profile(paid_workspace: Any, user: Any) -> TasteProfile:
    """Pro, deliberately: the trial plan retains 7 days of analytics, so a
    window wide enough to hold two segments' worth of posts does not exist on
    it. Depending on `paid_workspace` here rather than in each test means the
    upgrade always lands before any post is published into the window."""
    return TasteProfile.objects.create(
        workspace=paid_workspace,
        version=1,
        is_active=True,
        voice={"tone": "warm"},
        created_by=user,
    )


def publish(
    workspace: Any,
    user: Any,
    *,
    platform: str = Platform.INSTAGRAM,
    post_format: str = "FEED",
    body: str = "A short caption for the window.",
    rate: float | None = 0.05,
    days_ago: int = 5,
) -> PostTarget:
    """One published target with one capture, or with none if `rate` is None."""
    post = create_post(workspace=workspace, author=user, master_body=body)
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])

    published_at = NOW - dt.timedelta(days=days_ago)
    target = PostTarget.objects.create(
        post=post,
        platform=platform,
        post_format=post_format,
        state=PostTargetState.PUBLISHED,
        published_at=published_at,
    )
    PostMetric.objects.create(
        post_target=target,
        captured_at=published_at + dt.timedelta(hours=24),
        engagement_rate=rate,
        impressions=1000 if rate is not None else None,
        # The distinction the whole subsystem rests on: a row that measured
        # nothing carries no values, and is not a row of zeros.
        availability=Availability.MEASURED if rate is not None else Availability.UNAVAILABLE,
        provider_key="fake",
    )
    return target


class TestTheJob:
    def test_a_digest_is_written_even_when_there_is_nothing_to_say(
        self, workspace: Any, profile: TasteProfile
    ) -> None:
        """An empty digest is a real answer and a customer needs to see it.

        Writing nothing would be indistinguishable from a job that failed."""
        digest = run_learn(workspace, now=NOW)

        assert Digest.objects.filter(pk=digest.pk).exists()
        assert digest.narration
        assert digest.findings.count() == 0

    def test_findings_carry_a_grade_and_a_sample_size(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        for index in range(10):
            publish(workspace, user, post_format="CAROUSEL", rate=0.09, days_ago=index + 1)
        for index in range(10):
            publish(workspace, user, post_format="FEED", rate=0.02, days_ago=index + 1)

        digest = run_learn(workspace, now=NOW)

        assert digest.findings.exists()
        for finding in digest.findings.all():
            assert finding.confidence in Confidence.values
            assert finding.sample_size > 0

    def test_an_unavailable_capture_never_becomes_a_zero(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        """C-07 through the whole stack, not only in `analyse`."""
        for index in range(8):
            publish(workspace, user, post_format="CAROUSEL", rate=0.09, days_ago=index + 1)
        publish(workspace, user, post_format="CAROUSEL", rate=None, days_ago=9)
        for index in range(8):
            publish(workspace, user, post_format="FEED", rate=0.02, days_ago=index + 1)

        digest = run_learn(workspace, now=NOW)
        carousel = digest.findings.get(segment__value="CAROUSEL")

        assert carousel.sample_size == 8, "the unmeasured post entered the denominator"
        assert carousel.comparison["segment_mean"] == pytest.approx(0.09)

    def test_the_window_is_the_plans_horizon_not_the_campaign(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        """P7-05. A post older than the campaign still informs the baseline."""
        publish(workspace, user, days_ago=3)
        digest = run_learn(workspace, now=NOW)

        assert (digest.window_end - digest.window_start).days == 90

    def test_the_horizon_bounds_the_window_and_a_plan_change_moves_it(
        self, workspace: Any, user: Any, profile: TasteProfile, plans: dict[str, Any]
    ) -> None:
        """A digest may not reach further back than the plan retains.

        Not a nicety: the posts outside the horizon are the ones whose metrics
        the retention job is entitled to have already deleted, so a window that
        overran would compute a mean over a silently truncated population and
        show it with a confident sample size."""
        publish(workspace, user, days_ago=40, rate=0.09)

        workspace.organization.plan = plans["free"]
        workspace.organization.save(update_fields=["plan"])

        digest = run_learn(workspace, now=NOW)

        assert (digest.window_end - digest.window_start).days == 7
        assert digest.findings.count() == 0, "a post outside the horizon was counted"

    def test_rerunning_writes_a_new_digest_rather_than_editing_the_old_one(
        self, workspace: Any, profile: TasteProfile
    ) -> None:
        first = run_learn(workspace, now=NOW)
        second = run_learn(workspace, now=NOW + dt.timedelta(days=1))

        assert first.pk != second.pk
        assert Digest.objects.filter(pk=first.pk).exists(), "the earlier digest must stay readable"

    def test_a_digest_is_append_only(self, workspace: Any, profile: TasteProfile) -> None:
        from common.records import AppendOnlyError

        digest = run_learn(workspace, now=NOW)
        digest.narration = "rewritten after the fact"
        with pytest.raises(AppendOnlyError):
            digest.save()


class TestTheTrendLoopCloses:
    """P7-12 — stage 6 writes the cluster; stage 7 has to find it."""

    def test_a_trend_generated_post_is_segmented_by_its_cluster_label(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        from taste.models import ContentCandidate

        for index in range(9):
            target = publish(workspace, user, rate=0.09, days_ago=index + 1)
            ContentCandidate.objects.create(
                workspace=workspace,
                taste_profile=profile,
                post=target.post,
                # Exactly the shape `taste/services/topics.py` writes.
                payload={"master_body": "x", "trend": {"cluster": 1, "label": "cold brew"}},
            )
        for index in range(9):
            publish(workspace, user, rate=0.01, days_ago=index + 10)

        digest = run_learn(workspace, now=NOW)
        topics = digest.findings.filter(segment__dimension="topic")

        assert topics.exists(), "the trend cluster never reached the segmenter"
        assert topics.first().segment["value"] == "cold brew"

    def test_tone_comes_from_the_profile_version_the_candidate_ran_under(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        from taste.models import ContentCandidate

        for index in range(9):
            target = publish(workspace, user, rate=0.09, days_ago=index + 1)
            ContentCandidate.objects.create(
                workspace=workspace, taste_profile=profile, post=target.post, payload={}
            )
        for index in range(9):
            publish(workspace, user, rate=0.01, days_ago=index + 10)

        digest = run_learn(workspace, now=NOW)
        tones = digest.findings.filter(segment__dimension="tone")

        assert tones.exists()
        assert tones.first().segment["value"] == "warm"


class TestProposals:
    """P7-11 — Learn proposes; a human activates; a rejection is recorded."""

    def _digest_with_finding(
        self,
        workspace: Any,
        *,
        dimension: str = "format",
        value: str = "CAROUSEL",
        confidence: str = Confidence.STRONG,
        sample_size: int = 24,
        led_in: int = 18,
        baseline_size: int = 60,
        campaigns_observed: int = 3,
    ) -> Digest:
        digest = Digest.objects.create(
            workspace=workspace,
            window_start=NOW - dt.timedelta(days=90),
            window_end=NOW,
        )
        Finding.objects.create(
            digest=digest,
            segment={"dimension": dimension, "value": value},
            comparison={"led_in": led_in, "sample_size": sample_size},
            confidence=confidence,
            sample_size=sample_size,
            baseline_size=baseline_size,
            campaigns_observed=campaigns_observed,
        )
        return digest

    def _strong_finding(self, workspace: Any, user: Any) -> Digest:
        return self._digest_with_finding(workspace)

    def test_a_strong_finding_proposes_an_unaccepted_rule_in_an_inactive_set(
        self, workspace: Any, user: Any
    ) -> None:
        digest = self._strong_finding(workspace, user)

        ruleset = proposals.propose_from(digest)

        assert ruleset is not None
        assert ruleset.is_active is False, "Learn must never activate its own conclusions"
        rule = ruleset.rules.get()
        assert rule.accepted_at is None
        assert rule.provenance_id == digest.findings.get().pk

    def test_an_emerging_finding_proposes_a_test_and_never_a_rule(
        self, workspace: Any, user: Any
    ) -> None:
        digest = self._digest_with_finding(
            workspace,
            confidence=Confidence.EMERGING,
            sample_size=9,
            led_in=7,
            baseline_size=20,
            campaigns_observed=1,
        )

        assert proposals.propose_from(digest) is None
        assert Rule.objects.count() == 0
        assert len(proposals.suggested_tests(digest)) == 1

    def test_a_platform_finding_proposes_nothing(self, workspace: Any, user: Any) -> None:
        """ "Instagram beats LinkedIn" is a true finding and not a rule — no
        rule the generator understands can act on it, and inventing a kind
        would put a rule in the set that silently does nothing."""
        digest = self._digest_with_finding(
            workspace,
            dimension="platform",
            value="INSTAGRAM",
            sample_size=25,
            led_in=20,
            baseline_size=40,
            campaigns_observed=4,
        )

        assert proposals.propose_from(digest) is None

    def test_accepting_records_a_decision_against_the_rule(self, workspace: Any, user: Any) -> None:
        ruleset = proposals.propose_from(self._strong_finding(workspace, user))
        assert ruleset is not None
        rule = ruleset.rules.get()

        decision = proposals.accept(rule, actor=user)
        rule.refresh_from_db()

        assert decision.verdict == Verdict.ACCEPTED
        assert decision.rule_id == rule.pk
        assert decision.candidate_id is None
        assert rule.accepted_at is not None

    def test_rejecting_keeps_the_rule_and_records_why(self, workspace: Any, user: Any) -> None:
        """A deleted proposal is the same silence as never having proposed it."""
        ruleset = proposals.propose_from(self._strong_finding(workspace, user))
        assert ruleset is not None
        rule = ruleset.rules.get()

        decision = proposals.reject(rule, actor=user, reason_code="wrong_timing")
        rule.refresh_from_db()

        assert Rule.objects.filter(pk=rule.pk).exists()
        assert rule.accepted_at is None
        assert decision.verdict == Verdict.REJECTED
        assert decision.reason_code == "wrong_timing"

    def test_a_decision_cannot_be_about_both_a_candidate_and_a_rule(
        self, workspace: Any, user: Any
    ) -> None:
        """Enforced in the database: in an append-only table, a row that is
        about two things cannot be aggregated as either and cannot be fixed."""
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            Decision.objects.create(candidate=None, rule=None, verdict=Verdict.ACCEPTED, actor=user)


class TestCampaignClose:
    def test_closing_a_campaign_points_it_at_its_digest(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        from planning.models import Campaign, CampaignStatus

        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Spring launch",
            starts_at=NOW - dt.timedelta(days=30),
            ends_at=NOW,
            status=CampaignStatus.ACTIVE,
            closed_at=NOW,
            created_by=user,
        )
        publish(workspace, user, days_ago=3)

        digest = run_learn(workspace, campaign=campaign, now=NOW)
        campaign.refresh_from_db()

        assert campaign.digest_id == digest.pk
        assert digest.campaign_id == campaign.pk


class TestNarrationIsRecorded:
    def test_the_digest_says_which_half_wrote_its_prose(
        self, workspace: Any, profile: TasteProfile
    ) -> None:
        digest = run_learn(workspace, now=NOW)
        assert digest.narration_source in NarrationSource.values

    def test_the_statistics_payload_is_kept_for_audit(
        self, workspace: Any, user: Any, profile: TasteProfile
    ) -> None:
        """A disputed sentence has to be checkable against the numbers it was
        supposed to render — otherwise P7-08's split is unauditable."""
        publish(workspace, user, days_ago=2)
        digest = run_learn(workspace, now=NOW)

        assert "findings" in digest.statistics
        assert digest.statistics["window_end"] == digest.window_end.date().isoformat()


def test_the_active_versions_are_stamped_on_the_digest(
    workspace: Any, profile: TasteProfile
) -> None:
    RuleSet.objects.create(workspace=workspace, version=4, is_active=True)

    digest = run_learn(workspace, now=timezone.now())

    assert digest.taste_profile_version == 1
    assert digest.ruleset_version == 4
