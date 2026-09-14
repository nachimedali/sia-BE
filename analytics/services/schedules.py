"""Automatic monthly reports (P6-07).

**"Month" is a local word.** A workspace in Lisbon and one in Los Angeles do not
agree on when September ended, and a job that used UTC would send the Los
Angeles customer a report covering 17:00 on the 31st to 17:00 on the 30th —
wrong by eight hours on both ends, every month, invisibly. So the boundary is
computed in the workspace's own IANA zone and converted to UTC at the edge,
which is the rule the timetables already follow (Part 3: store UTC, convert at
edges).

**Due-based, not self-scheduling**, the same shape as the capture ladder: the
beat asks which reports are owed a run rather than each run queueing the next
one. A worker down over a month boundary produces the missing report on the
next tick instead of losing it, and a schedule changed in the UI takes effect
immediately rather than after the queued task drains.

**Idempotent by the run it would create.** A report is owed only if no run
already covers that month's window, so the job can tick every hour of the
first day of the month and produce exactly one document.
"""

from __future__ import annotations

import datetime as dt
import logging
import zoneinfo
from typing import TYPE_CHECKING

from django.utils import timezone

from analytics.models import Report, ReportRun, ReportSchedule
from analytics.services.reporting import HorizonExceeded, render_report

if TYPE_CHECKING:
    from workspaces.models import Workspace

logger = logging.getLogger(__name__)


def _zone(workspace: Workspace) -> dt.tzinfo:
    """The workspace's zone, or UTC when it carries none.

    UTC rather than the server's local zone: a server default would make the
    boundary depend on where the process happens to run, which is the one thing
    a month boundary must not do.
    """
    try:
        return zoneinfo.ZoneInfo(workspace.timezone or "UTC")
    except zoneinfo.ZoneInfoNotFoundError:
        logger.warning("unknown workspace timezone", extra={"workspace_id": workspace.pk})
        return dt.UTC


def last_month(
    workspace: Workspace, *, now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """The previous calendar month in this workspace's zone, as UTC instants.

    Half-open at the top — `[first of last month, first of this month)` — so a
    post published at 23:59:59 on the 31st belongs to exactly one month and no
    post belongs to two.
    """
    zone = _zone(workspace)
    local_now = (now or timezone.now()).astimezone(zone)
    this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = (this_month - dt.timedelta(days=1)).replace(day=1)
    return previous.astimezone(dt.UTC), this_month.astimezone(dt.UTC)


def is_due(report: Report, *, now: dt.datetime | None = None) -> bool:
    """Owed a run for last month, and not already given one."""
    if report.schedule != ReportSchedule.MONTHLY:
        return False
    starts_at, ends_at = last_month(report.workspace, now=now)
    return not ReportRun.objects.filter(
        report=report, window_start=starts_at, window_end=ends_at
    ).exists()


def run_due(*, now: dt.datetime | None = None) -> int:
    """Render every monthly report that is owed one. Returns how many ran.

    **A horizon refusal is skipped, not raised** (P6-09). A workspace whose plan
    covers seven days cannot have a monthly report, and the honest outcome is
    that no document is produced — not a `FAILED` run the customer finds in a
    list, and certainly not a quietly truncated month. The refusal is logged so
    the reason is recoverable.
    """
    ran = 0
    for report in Report.objects.filter(schedule=ReportSchedule.MONTHLY).select_related(
        "workspace", "workspace__organization"
    ):
        if not is_due(report, now=now):
            continue
        starts_at, ends_at = last_month(report.workspace, now=now)
        try:
            render_report(report, starts_at=starts_at, ends_at=ends_at)
        except HorizonExceeded:
            logger.info(
                "monthly report outside the plan horizon",
                extra={"report_id": report.pk, "workspace_id": report.workspace_id},
            )
            continue
        ran += 1
    return ran
