"""A campaign's progress against its goal (steps-plan S5).

**Measured, or it is a gap.** The total is `analytics.services.reporting.
campaign_totals` — the newest analysable reading of each post in the campaign —
so a fake-adapter row never counts and an unreported metric is `None`, never 0.

The daily series answers "what was measured that day". A day on which no post
in the campaign was read is `None`: a gap, drawn as a gap. A day that was read
carries the growth since the previous read day, so a reading after a gap holds
everything since the last one — it is attributed to the day it was measured,
not spread across days nobody measured.

No percentage, pace or end-of-campaign figure is computed here. The page may
put the measured number beside the user's own target; anything past that is a
projection (Part 7 rule 15).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.utils import timezone

from analytics.models import PostMetric
from analytics.services.reporting import SUMMABLE, campaign_totals
from planning.models import Campaign
from planning.services.briefs import METRIC_NOUNS, goal_of


def _end_of(campaign: Campaign) -> dt.datetime:
    return min(timezone.now(), campaign.closed_at or campaign.ends_at)


def summary(campaign: Campaign) -> dict[str, Any]:
    goal = goal_of(campaign)
    now = timezone.now()
    base: dict[str, Any] = {
        "metric": goal["metric"] if goal else None,
        "label": METRIC_NOUNS.get(goal["metric"], goal["metric"]) if goal else None,
        "target": goal["target"] if goal else None,
        "actual": None,
        "measured_posts": 0,
        "measured_to": None,
        "day": None,
        "days_total": (campaign.ends_at.date() - campaign.starts_at.date()).days + 1,
    }
    if goal is None:
        return {**base, "state": "no_goal"}
    if now < campaign.starts_at:
        return {**base, "state": "not_started"}

    totals = campaign_totals(campaign)
    actual = totals.get(goal["metric"])
    newest = (
        PostMetric.objects.analysable()
        .filter(post_target__post__campaign_items__campaign=campaign)
        .order_by("-captured_at")
        .values_list("captured_at", flat=True)
        .first()
    )
    return {
        **base,
        "actual": actual,
        "measured_posts": totals["posts"],
        "measured_to": newest,
        "day": min(
            (_end_of(campaign).date() - campaign.starts_at.date()).days + 1, base["days_total"]
        ),
        "state": "not_enough_data" if actual is None else "measured",
    }


def daily(campaign: Campaign, metric: str | None) -> list[dict[str, Any]]:
    """One entry per day from the start to today (or the close), UTC."""
    first = campaign.starts_at.date()
    last = _end_of(campaign).date()
    if last < first:
        return []
    days = [first + dt.timedelta(days=n) for n in range((last - first).days + 1)]
    if metric not in SUMMABLE:
        return [{"date": day.isoformat(), "value": None} for day in days]

    rows = (
        PostMetric.objects.analysable()
        .filter(
            post_target__post__campaign_items__campaign=campaign,
            captured_at__lte=_end_of(campaign),
        )
        .exclude(**{f"{metric}__isnull": True})
        .order_by("captured_at")
        .values_list("post_target", "captured_at", metric)
    )

    latest: dict[int, int] = {}
    read_on: dict[dt.date, int] = {}
    for target, captured_at, value in rows:
        latest[target] = value
        # The cumulative total as of this reading, kept per day so the day's
        # last reading is the one that stands.
        read_on[captured_at.astimezone(dt.UTC).date()] = sum(latest.values())

    out: list[dict[str, Any]] = []
    previous = 0
    for day in days:
        if day in read_on:
            out.append({"date": day.isoformat(), "value": read_on[day] - previous})
            previous = read_on[day]
        else:
            out.append({"date": day.isoformat(), "value": None})
    return out


def detail(campaign: Campaign) -> dict[str, Any]:
    head = summary(campaign)
    days = (
        daily(campaign, head["metric"]) if head["state"] in {"measured", "not_enough_data"} else []
    )
    return {**head, "days": days, "gap_days": sum(1 for day in days if day["value"] is None)}
