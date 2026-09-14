"""P6-G1 — **a report refuses an out-of-horizon window.**

Phase 6's ship gate is one sentence in BUILD-PLAN, and it is the one thing in
this phase that cannot be softened later: *"Reports refuse to render beyond the
horizon rather than silently truncating — a client-facing PDF with a quietly
clipped range is worse than an error."*

The asymmetry is what makes it a gate. An error is recoverable: somebody sees
it, shortens the range or upgrades. A plausible document is not — it is
forwarded, quoted and acted on, and nothing in it says which half of the range
is missing.

Three paths can produce a document, so the refusal is asserted on all three:
the API, the monthly beat job, and the service every other caller goes
through. A gate that held on one of them would hold on the one nobody uses.

The second half of this file is the other claim the phase makes: **a number a
customer reads is never fabricated** (Part 7 rules 12 and 17). Unavailable is
null across every Phase 6 surface — demographics, channel totals, campaign
goals and the competitor comparison — and a fake-adapter row reaches none of
them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.models import (
    Availability,
    MetricSource,
    PostMetric,
    Report,
    ReportRun,
    ReportSchedule,
)
from analytics.services import reporting, schedules
from billing.services.entitlements import entitlements_for
from common.exceptions import PaymentRequired
from content.models import Platform, PostStatus, PostTarget
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


def _report(workspace: Any, user: Any, **fields: Any) -> Report:
    return Report.objects.create(
        workspace=workspace,
        name=fields.pop("name", "Client report"),
        sections=[{"kind": "channel_totals"}],
        created_by=user,
        **fields,
    )


# -----------------------------------------------------------------------------
# The gate
# -----------------------------------------------------------------------------
class TestTheHorizonRefusal:
    def test_the_service_refuses_rather_than_clipping(self, workspace: Any, user: Any) -> None:
        """The trial plan keeps seven days. A ninety-day report is not a
        shorter report; it is a report this plan cannot produce."""
        horizon = entitlements_for(workspace).analytics_horizon_days()
        assert horizon < 90

        ends_at = timezone.now()
        with pytest.raises(PaymentRequired):
            reporting.render_report(
                _report(workspace, user),
                starts_at=ends_at - dt.timedelta(days=90),
                ends_at=ends_at,
            )

    def test_nothing_is_written_when_the_window_is_refused(self, workspace: Any, user: Any) -> None:
        """**No run row, not even a failed one.** A `ReportRun` in the list is
        a document somebody will try to open."""
        report = _report(workspace, user)
        ends_at = timezone.now()

        with pytest.raises(PaymentRequired):
            reporting.render_report(
                report, starts_at=ends_at - dt.timedelta(days=90), ends_at=ends_at
            )

        assert ReportRun.objects.filter(report=report).count() == 0

    def test_the_api_answers_402_with_an_upgrade_path(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        """402, not 400: the caller's request is well-formed and their plan is
        what refuses it, so the envelope carries somewhere to go."""
        report = _report(workspace, user)
        ends_at = timezone.now()

        response = auth_client.post(
            reverse("report-render", args=[report.pk]),
            {
                "starts_at": (ends_at - dt.timedelta(days=90)).isoformat(),
                "ends_at": ends_at.isoformat(),
            },
            format="json",
        )

        assert response.status_code == 402
        assert response.data["error"]["code"] == "analytics_horizon_exceeded"

    def test_the_monthly_job_produces_nothing_it_cannot_honour(
        self, workspace: Any, user: Any
    ) -> None:
        """The scheduled path is where a clipped document would be least
        visible — nobody asked for it, so nobody is watching when it lands."""
        report = _report(workspace, user, schedule=ReportSchedule.MONTHLY)

        assert schedules.run_due() == 0
        assert ReportRun.objects.filter(report=report).count() == 0

    def test_a_window_the_plan_covers_renders(self, paid_workspace: Any, user: Any) -> None:
        """The gate refuses what cannot be honoured — not everything. A gate
        that failed closed on valid windows would be indistinguishable from a
        broken feature."""
        report = _report(paid_workspace, user)
        ends_at = timezone.now()

        run = reporting.render_report(
            report, starts_at=ends_at - dt.timedelta(days=30), ends_at=ends_at
        )

        assert run.status == "READY"
        assert run.document


# -----------------------------------------------------------------------------
# No fabricated number reaches a reader
# -----------------------------------------------------------------------------
class TestUnavailableIsNeverZero:
    """Part 7 rules 12 and 17, across every surface this phase added."""

    def test_a_platform_that_reported_nothing_is_absent_not_zero(
        self, paid_workspace: Any, user: Any
    ) -> None:
        """ "We published nothing there" and "we measured nothing there" are
        different facts. A zero row states the first while meaning the second."""
        post = create_post(workspace=paid_workspace, author=user, master_body="Hello")
        post.status = PostStatus.PUBLISHED
        post.save(update_fields=["status"])
        target = PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)
        PostMetric.objects.create(
            post_target=target,
            captured_at=timezone.now(),
            availability=Availability.UNAVAILABLE,
            source=MetricSource.PROVIDER,
            provider_key="zernio",
        )

        rows = reporting.channel_totals(
            paid_workspace,
            starts_at=timezone.now() - dt.timedelta(days=7),
            ends_at=timezone.now(),
        )

        assert rows == []

    def test_a_fake_row_never_reaches_a_report(self, paid_workspace: Any, user: Any) -> None:
        """The fake adapter is a test fixture. A document built from one would
        be a fabricated number a customer acts on."""
        post = create_post(workspace=paid_workspace, author=user, master_body="Hello")
        post.status = PostStatus.PUBLISHED
        post.save(update_fields=["status"])
        target = PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)
        PostMetric.objects.create(
            post_target=target,
            captured_at=timezone.now(),
            impressions=9_999,
            likes=9_999,
            availability=Availability.MEASURED,
            source=MetricSource.FAKE,
            provider_key="fake",
        )

        rows = reporting.channel_totals(
            paid_workspace,
            starts_at=timezone.now() - dt.timedelta(days=7),
            ends_at=timezone.now(),
        )

        assert rows == []

    def test_a_goal_on_an_unmeasured_metric_is_not_reported_as_missed(
        self, paid_workspace: Any, user: Any
    ) -> None:
        """Saying a goal was missed when nothing was measured is a fabricated
        verdict against the customer — `met` is `None`, not `False`."""
        from planning.models import Campaign

        campaign = Campaign.objects.create(
            workspace=paid_workspace,
            name="Autumn",
            starts_at=timezone.now() - dt.timedelta(days=14),
            ends_at=timezone.now(),
            goals=[{"metric": "impressions", "target": 10_000}],
        )

        result = reporting.campaign_totals(campaign)

        assert result["goals"][0]["met"] is None
        assert result["goals"][0]["actual"] is None

    def test_a_demographic_the_provider_declined_is_null_not_an_empty_chart(
        self, social_account: Any, metrics_provider: Any
    ) -> None:
        from analytics.models import AudienceDemographic
        from analytics.services import ingest

        metrics_provider.tiny_accounts.add(social_account.provider_account_id)
        ingest.snapshot_accounts()

        rows = AudienceDemographic.objects.all()
        assert rows.exists()
        assert all(row.breakdown is None for row in rows)
        assert all(row.availability == Availability.UNAVAILABLE for row in rows)

    def test_a_competitor_with_no_reported_audience_has_a_null_rate(
        self, paid_workspace: Any
    ) -> None:
        """Part 7 rule 12 applies to somebody else's account too."""
        from categories.models import Category
        from trends.models import TrendItem
        from trends.services import competitors

        paid_workspace.category = Category.objects.create(name="Ceramics", slug="cer")
        paid_workspace.save(update_fields=["category"])
        source = competitors.track(paid_workspace, platform=Platform.INSTAGRAM, handle="rival")
        competitors.refresh(paid_workspace, platform=Platform.INSTAGRAM)
        TrendItem.objects.filter(source=source).update(author_followers=0)

        result = competitors.comparison(paid_workspace, platform=Platform.INSTAGRAM)

        assert result["competitors"][0]["engagement_rate"] is None


# -----------------------------------------------------------------------------
# The flag
# -----------------------------------------------------------------------------
def test_flag_off_restores_pre_phase_behaviour(
    auth_client: Any, paid_workspace: Any, user: Any
) -> None:
    """Part 3: flag off produces pre-phase behaviour, not an error.

    Before Phase 6 there were no reports, so the surface answers as it did
    then — a refusal from the gate, never a 500.
    """
    from billing.models import FeatureFlag
    from billing.services.flags import ANALYTICS_V6

    FeatureFlag.objects.create(
        organization=paid_workspace.organization, key=ANALYTICS_V6, enabled=False
    )
    report = _report(paid_workspace, user)

    response = auth_client.post(reverse("report-render", args=[report.pk]), {}, format="json")

    assert response.status_code == 404
    assert ReportRun.objects.filter(report=report).count() == 0
