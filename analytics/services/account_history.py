"""An account's daily history, kept as the provider reported it (SA-52).

Followers, reach, profile views, Google's search and maps views — every series
the provider publishes for an account, one row per metric per day
(`AccountMetricDay`). Read on the daily account pass, never on a ladder of its
own.

**Gaps stay gaps** (plan §5.3 rule 4). A gauge's change is spread over the days
it spans instead of being drawn as one day's spike, and nothing is
interpolated; a day on which fewer subjects reported than the account usually
counts is flagged `partial`, because a lower sum on that day is missing data,
not a decline.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from django.db.models import Max
from django.utils import timezone

from analytics.models import AccountMetricDay, AccountMetricKind
from analytics.providers.base import MetricsError
from channels.models import SocialAccount

logger = logging.getLogger(__name__)

#: How far back the first read reaches (the provider's default window).
BACKFILL_DAYS = 30
#: How far back every later read reaches: long enough to pick up the days the
#: provider re-cut since the last run and any day a skipped run missed.
REREAD_DAYS = 7


@dataclass(frozen=True)
class SeriesPoint:
    day: dt.date
    value: int | None
    #: What the day contributed; `None` where the provider gave nothing.
    per_day: float | None
    #: More than one day covered by this point's change: drawn as a gap.
    spanned: int
    partial: bool


def capture_history(account: SocialAccount, provider: Any, *, today: dt.date) -> int:
    """Reads the account's recent history and keeps the latest of each day.
    Returns the rows written. A provider without history writes nothing."""
    fetch = getattr(provider, "fetch_account_history", None)
    if fetch is None:
        return 0
    has_history = AccountMetricDay.objects.filter(social_account=account).exists()
    start = today - dt.timedelta(days=(REREAD_DAYS if has_history else BACKFILL_DAYS) - 1)
    try:
        days = fetch(
            platform=account.platform,
            provider_account_id=account.provider_account_id,
            start=start,
            end=today,
        )
    except MetricsError:
        logger.warning("account history failed", exc_info=True, extra={"account_id": account.pk})
        return 0
    if not days:
        return 0

    usual = dict(
        AccountMetricDay.objects.filter(social_account=account)
        .values("metric")
        .annotate(most=Max("subjects_counted"))
        .values_list("metric", "most")
    )
    for point in days:
        usual[point.metric] = max(usual.get(point.metric) or 1, point.subjects_counted)

    moment = timezone.now()
    for point in days:
        AccountMetricDay.objects.update_or_create(
            social_account=account,
            metric=point.metric,
            day=point.day,
            defaults={
                "kind": point.kind,
                "value": point.value,
                "change": point.change,
                "days_spanned": point.days_spanned,
                "subjects_counted": point.subjects_counted,
                "partial": point.subjects_counted < usual[point.metric],
                "day_tz": point.day_tz,
                "source": point.source,
                "provider_key": getattr(provider, "key", ""),
                "captured_at": moment,
            },
        )
    return len(days)


def series(
    account: SocialAccount, metric: str, *, start: dt.date, end: dt.date
) -> list[SeriesPoint]:
    """One metric's days in `[start, end]`, oldest first, as stored."""
    return [
        SeriesPoint(
            day=row.day,
            value=row.value,
            per_day=row.per_day,
            spanned=row.days_spanned,
            partial=row.partial,
        )
        for row in AccountMetricDay.objects.filter(
            social_account=account, metric=metric, day__gte=start, day__lte=end
        ).order_by("day")
    ]


def net_change(account: SocialAccount, metric: str, *, start: dt.date, end: dt.date) -> int | None:
    """How much a level moved over `(start, end]`, or a flow's total over
    `[start, end]`. `None` when no day in the range carries a figure — an
    account with no history has no change, not a change of zero."""
    rows = list(
        AccountMetricDay.objects.filter(
            social_account=account, metric=metric, day__gte=start, day__lte=end
        )
    )
    if not rows:
        return None
    if rows[0].kind == AccountMetricKind.GAUGE:
        changes = [row.change for row in rows if row.day > start and row.change is not None]
    else:
        changes = [row.value for row in rows if row.value is not None]
    return sum(changes) if changes else None
