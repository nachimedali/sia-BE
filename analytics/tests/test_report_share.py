"""Report share links and the monthly schedule (P6-06, P6-07).

The share link is the Phase 2 `GUEST_VIEW` pattern applied to a rendered run,
so what is tested here is what differs: it points at a **frozen** run rather
than a live definition, it refuses a run that is not ready, and it has no
approve path at all.

The schedule's whole difficulty is one word. "Last month" is a local statement,
and a job that computed it in UTC would be wrong by the zone offset on both
ends of every window it ever produced — quietly, and in a document a client
reads.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from analytics.models import Report, ReportRun, ReportRunStatus, ReportSchedule, ReportShareLink
from analytics.services import report_share, schedules

pytestmark = pytest.mark.django_db


@pytest.fixture
def report(workspace: Any, user: Any) -> Report:
    return Report.objects.create(
        workspace=workspace,
        name="Monthly performance",
        sections=[{"kind": "channel_totals"}],
        created_by=user,
    )


@pytest.fixture
def run(report: Report) -> ReportRun:
    return ReportRun.objects.create(
        report=report,
        window_start=timezone.now() - dt.timedelta(days=30),
        window_end=timezone.now(),
        status=ReportRunStatus.READY,
        payload={"report": report.name, "sections": []},
    )


# -----------------------------------------------------------------------------
# Share links
# -----------------------------------------------------------------------------
class TestSharing:
    def test_only_the_digest_is_stored(self, run: ReportRun, user: Any) -> None:
        """The raw token exists once, in the message that carries it.

        A database leak that hands somebody a working link is the failure this
        whole pattern exists to prevent.
        """
        link, raw = report_share.issue(run, created_by=user)

        assert raw not in link.token_hash
        assert ReportShareLink.objects.filter(token_hash=raw).count() == 0
        assert report_share.resolve(raw) == link

    def test_an_unknown_token_resolves_to_nothing(self, run: ReportRun, user: Any) -> None:
        report_share.issue(run, created_by=user)
        assert report_share.resolve("not-a-real-token") is None

    def test_a_revoked_link_stops_working(self, run: ReportRun, user: Any) -> None:
        """Multi-use is the deviation, and revoke is why it is acceptable: a
        link that cannot expire by being consumed needs another way to close."""
        link, raw = report_share.issue(run, created_by=user)
        report_share.revoke(link)

        assert report_share.resolve(raw) is None

    def test_revoking_twice_is_not_an_error(self, run: ReportRun, user: Any) -> None:
        link, _raw = report_share.issue(run, created_by=user)
        first = report_share.revoke(link).revoked_at
        assert report_share.revoke(link).revoked_at == first

    def test_an_expired_link_stops_working(self, run: ReportRun, user: Any) -> None:
        link, raw = report_share.issue(run, created_by=user)
        link.expires_at = timezone.now() - dt.timedelta(seconds=1)
        link.save(update_fields=["expires_at"])

        assert report_share.resolve(raw) is None

    def test_multi_use_within_its_life(self, run: ReportRun, user: Any) -> None:
        """Unlike an `APPROVE` link, reading a report does not spend it."""
        _link, raw = report_share.issue(run, created_by=user)

        assert report_share.resolve(raw) is not None
        assert report_share.resolve(raw) is not None

    def test_resending_to_the_same_address_closes_the_earlier_link(
        self, run: ReportRun, user: Any
    ) -> None:
        """The verification-email rule: leaving the first link live widens the
        window on one that may have gone to a mistyped address."""
        _first, first_raw = report_share.issue(run, created_by=user, email="client@example.com")
        _second, second_raw = report_share.issue(run, created_by=user, email="client@example.com")

        assert report_share.resolve(first_raw) is None
        assert report_share.resolve(second_raw) is not None

    def test_a_pending_run_cannot_be_shared(self, report: Report, user: Any) -> None:
        """A link to a render that has not happened is a link to a blank page."""
        pending = ReportRun.objects.create(
            report=report,
            window_start=timezone.now() - dt.timedelta(days=7),
            window_end=timezone.now(),
            status=ReportRunStatus.PENDING,
        )
        with pytest.raises(ValueError, match="rendered"):
            report_share.issue(pending, created_by=user)

    def test_a_failed_run_cannot_be_shared(self, report: Report, user: Any) -> None:
        failed = ReportRun.objects.create(
            report=report,
            window_start=timezone.now() - dt.timedelta(days=7),
            window_end=timezone.now(),
            status=ReportRunStatus.FAILED,
        )
        with pytest.raises(ValueError, match="rendered"):
            report_share.issue(failed, created_by=user)

    def test_the_guest_sees_the_frozen_payload_not_a_recomputation(
        self, run: ReportRun, user: Any
    ) -> None:
        """A figure somebody quoted in a meeting must still say what it said."""
        link, _raw = report_share.issue(run, created_by=user)
        context = report_share.guest_context(link)

        run.report.name = "Renamed after sharing"
        run.report.save(update_fields=["name"])

        assert context["payload"] == run.payload
        assert context["report_name"] == "Monthly performance"

    def test_an_emailed_share_sends_exactly_one_message(self, run: ReportRun, user: Any) -> None:
        from common.mail import _fake_sender as sender

        sender.outbox.clear()
        report_share.issue(run, created_by=user, email="Client@Example.com ")

        assert len(sender.outbox) == 1
        assert sender.outbox[0].to == "client@example.com"

    def test_a_link_with_no_address_sends_nothing(self, run: ReportRun, user: Any) -> None:
        """ "Copy link" is a real flow, and it is not an email."""
        from common.mail import _fake_sender as sender

        sender.outbox.clear()
        link, raw = report_share.issue(run, created_by=user)

        assert sender.outbox == []
        assert report_share.resolve(raw) == link


# -----------------------------------------------------------------------------
# Monthly schedule
# -----------------------------------------------------------------------------
class TestTheMonthBoundary:
    """P6-07 — "month" is a local word, and the job that forgets it is wrong
    by the zone offset on both ends, every month, invisibly."""

    def test_the_window_is_the_workspaces_own_month(self, workspace: Any) -> None:
        workspace.timezone = "America/Los_Angeles"
        workspace.save(update_fields=["timezone"])

        starts_at, ends_at = schedules.last_month(
            workspace, now=dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)
        )

        # 1 August 00:00 in Los Angeles is 07:00 UTC — not midnight UTC.
        assert starts_at == dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
        assert ends_at == dt.datetime(2026, 9, 1, 7, 0, tzinfo=dt.UTC)

    def test_a_workspace_east_of_utc_gets_its_own_boundary(self, workspace: Any) -> None:
        workspace.timezone = "Europe/Lisbon"
        workspace.save(update_fields=["timezone"])

        starts_at, _ends_at = schedules.last_month(
            workspace, now=dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)
        )
        # Lisbon is UTC+1 in August: local midnight is 23:00 UTC the day before.
        assert starts_at == dt.datetime(2026, 7, 31, 23, 0, tzinfo=dt.UTC)

    def test_january_reaches_back_into_the_previous_year(self, workspace: Any) -> None:
        workspace.timezone = "UTC"
        workspace.save(update_fields=["timezone"])

        starts_at, ends_at = schedules.last_month(
            workspace, now=dt.datetime(2026, 1, 9, 3, 0, tzinfo=dt.UTC)
        )
        assert starts_at == dt.datetime(2025, 12, 1, tzinfo=dt.UTC)
        assert ends_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def test_an_unknown_timezone_falls_back_to_utc_not_the_servers_zone(
        self, workspace: Any
    ) -> None:
        """A server default would make the boundary depend on where the process
        happens to run, which is the one thing it must not do."""
        workspace.timezone = "Mars/Olympus_Mons"
        workspace.save(update_fields=["timezone"])

        starts_at, _ends_at = schedules.last_month(
            workspace, now=dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)
        )
        assert starts_at == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


class TestRunningDueReports:
    def test_an_on_demand_report_is_never_due(self, report: Report) -> None:
        assert report.schedule == ReportSchedule.NONE
        assert schedules.is_due(report) is False

    def test_a_monthly_report_runs_once_however_often_the_job_ticks(
        self, report: Report, paid_workspace: Any
    ) -> None:
        """Hourly ticks, one document. `is_due` is the whole of that."""
        report.workspace = paid_workspace
        report.schedule = ReportSchedule.MONTHLY
        report.save(update_fields=["workspace", "schedule"])

        with time_machine.travel(dt.datetime(2026, 9, 1, 3, 35, tzinfo=dt.UTC), tick=False):
            assert schedules.run_due() == 1
            assert schedules.run_due() == 0
            assert schedules.run_due() == 0

        assert ReportRun.objects.filter(report=report).count() == 1

    def test_the_next_month_is_owed_a_new_run(self, report: Report, paid_workspace: Any) -> None:
        report.workspace = paid_workspace
        report.schedule = ReportSchedule.MONTHLY
        report.save(update_fields=["workspace", "schedule"])

        with time_machine.travel(dt.datetime(2026, 8, 1, 3, 35, tzinfo=dt.UTC), tick=False):
            schedules.run_due()
        with time_machine.travel(dt.datetime(2026, 9, 1, 3, 35, tzinfo=dt.UTC), tick=False):
            schedules.run_due()

        assert ReportRun.objects.filter(report=report).count() == 2

    def test_a_plan_that_cannot_cover_a_month_produces_no_document(
        self, report: Report, workspace: Any
    ) -> None:
        """P6-09, on the scheduled path.

        A seven-day horizon cannot honour a monthly report. The honest outcome
        is no document — not a `FAILED` run a customer finds in a list, and
        certainly not a month quietly clipped to a week.
        """
        report.schedule = ReportSchedule.MONTHLY
        report.save(update_fields=["schedule"])

        with time_machine.travel(dt.datetime(2026, 9, 1, 3, 35, tzinfo=dt.UTC), tick=False):
            assert schedules.run_due() == 0

        assert ReportRun.objects.filter(report=report).count() == 0
