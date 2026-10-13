"""The analytics page in one read (`analytics.html`, SA-70): totals, the daily
chart, the account strip and every published post's cells, for one window.

**Every figure is one of three things, and says which.** A cell is *measured*
(a number — and a measured 0 is a real 0), *pending* (the platform reports it
later, or the first capture has not run yet) or *unavailable* (the platform or
the connection cannot report it, with the reason). Nothing here ever turns the
second or third into a zero, and totals sum measured values only, naming the
accounts they could not include.

**Only real provider rows count** — `PostMetric.objects.analysable()`, the
same queryset every statistic reads. A fake-adapter row is never shown as a
number (Part 7 rule 17).

**The daily chart is by publish day.** Captures are cumulative per post, not
daily deltas, so a day's bar is the latest figure of every post published that
day. The page says so; it never presents that as "reach on that day".

**The window is bounded by the plan** (`analytics_history_days`). A window
reaching further back is refused with the same 402 a report gets (rule 14) —
the page offers longer windows as locked, never clipped.

Statistics are computed here, in code: the totals, the like-for-like change
and the engagement rate. Nothing is estimated or back-filled.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from analytics.models import Availability, MetricCapability, PostMetric
from analytics.services.ingest import LADDER_HOURS, RUNG_TOLERANCE
from analytics.services.reporting import ensure_within_horizon
from billing.services.entitlements import entitlements_for
from channels.models import SocialAccount, SocialAccountStatus
from content.models import Platform, PostTarget, PostTargetState
from workspaces.models import Workspace

#: Windows the page offers. Anything longer than the plan keeps is shown locked.
WINDOWS: tuple[int, ...] = (7, 28, 90, 180, 365)
DEFAULT_WINDOW = 28

#: The columns the page shows, in its order.
METRICS: tuple[str, ...] = ("reach", "views", "likes", "comments", "shares", "saves")
#: What "engagement" sums, where each is measured.
ENGAGEMENT: tuple[str, ...] = ("likes", "comments", "shares", "saves")
#: The four totals across the top.
TOTALS: tuple[str, ...] = ("reach", "views", "engagement", "comments")
#: Metrics an Instagram account connected without insights access cannot read.
INSIGHT_METRICS = frozenset({"reach", "views", "saves"})
#: Accounts whose numbers stopped at the last sync.
STALE_STATUSES = frozenset(
    {
        SocialAccountStatus.REAUTH_REQUIRED,
        SocialAccountStatus.PAGE_REMOVED,
        SocialAccountStatus.REVOKED,
    }
)
#: How many rows the posts table carries at most — newest first.
POSTS_LIMIT = 500

LADDER_LABELS = {1: "1 h", 6: "6 h", 24: "24 h", 72: "72 h", 168: "7 days"}


@dataclass
class Cell:
    state: str  # "measured" | "pending" | "unavailable"
    value: int | float | None = None
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "value": self.value, "why": self.why}


@dataclass
class _Capability:
    available: bool
    delay_hours: int
    note: str


@dataclass
class _Row:
    """One published target with what we know about it."""

    target: PostTarget
    account: SocialAccount | None
    cells: dict[str, Cell] = field(default_factory=dict)
    capture: str = "complete"
    capture_why: str = ""
    snapshots: list[dict[str, Any]] = field(default_factory=list)


def default_window(history: int) -> int:
    """The largest offered window up to 28 days that the plan keeps — 7 when
    it keeps less than a week, so a plan of any length opens on a real window."""
    fits = [w for w in WINDOWS if w <= DEFAULT_WINDOW and (history < 0 or w <= history)]
    return max(fits) if fits else WINDOWS[0]


def resolve_window(workspace: Workspace, requested: int | None) -> tuple[int, int]:
    """`(window, history_days)`. Refuses a window the page does not offer; one
    the plan does not keep is refused by `ensure_within_horizon` (rule 14)."""
    history = entitlements_for(workspace).analytics_horizon_days()
    window = requested or default_window(history)
    if window not in WINDOWS:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"window": f"Choose one of {', '.join(map(str, WINDOWS))} days."})
    return window, history


def _capabilities(provider_keys: set[str]) -> dict[tuple[str, str, str], _Capability]:
    rows = MetricCapability.objects.filter(provider_key__in=provider_keys)
    return {
        (row.provider_key, row.platform, row.metric_key): _Capability(
            available=row.available,
            delay_hours=row.delay_hours,
            note=row.note,
        )
        for row in rows
    }


def _latest_and_rungs(
    targets: list[PostTarget],
) -> tuple[dict[int, PostMetric], dict[int, set[dt.datetime]]]:
    """The newest analysable capture per target, and every rung answered
    (any availability — an unavailable answer is still an answer)."""
    latest: dict[int, PostMetric] = {
        row.post_target_id: row
        for row in PostMetric.objects.analysable()
        .filter(post_target__in=targets)
        .order_by("post_target_id", "-captured_at")
        .distinct("post_target_id")
        .defer("raw", "raw_payload")
    }
    answered: dict[int, set[dt.datetime]] = defaultdict(set)
    for target_id, captured_at in (
        PostMetric.objects.real()
        .filter(post_target__in=targets)
        .values_list("post_target_id", "captured_at")
    ):
        answered[target_id].add(captured_at)
    return latest, answered


def _cell(
    *,
    metric: str,
    target: PostTarget,
    account: SocialAccount | None,
    capture: PostMetric | None,
    capability: _Capability | None,
    now: dt.datetime,
    platform_name: str,
) -> Cell:
    if capability is not None and not capability.available:
        return Cell("unavailable", why=capability.note or f"Not reported by {platform_name}")
    if (
        account is not None
        and metric in INSIGHT_METRICS
        and account.capabilities.get("insights") is False
    ):
        return Cell(
            "unavailable",
            why=f"Reconnect {account.handle or platform_name} to enable reach and views",
        )
    published = target.published_at or now
    age_hours = (now - published).total_seconds() / 3600
    delay = capability.delay_hours if capability else 0
    if delay and age_hours < delay:
        ready_in = max(1, round(delay - age_hours))
        return Cell(
            "pending",
            why=f"{platform_name} finalises {metric} {delay} h after posting · ready in about "
            f"{ready_in} h",
        )
    if capture is None:
        return Cell("pending", why="The first numbers arrive an hour after publishing")
    if capture.availability != Availability.MEASURED:
        return Cell("unavailable", why=f"{platform_name} answered without numbers for this post")
    value = getattr(capture, metric, None)
    if value is None:
        return Cell("unavailable", why=f"{platform_name} did not report {metric} for this post")
    return Cell("measured", value=int(value))


def _engagement_rate(cells: dict[str, Cell]) -> Cell:
    reach = cells["reach"]
    if reach.state != "measured":
        why = (
            "Needs reach, which is still pending"
            if reach.state == "pending"
            else f"Needs reach, which is unavailable: {reach.why}"
        )
        return Cell(reach.state, why=why)
    if not reach.value:
        return Cell("unavailable", why="Reach was 0, so a rate would divide by zero")
    engaged = sum(int(cells[m].value or 0) for m in ENGAGEMENT if cells[m].state == "measured")
    return Cell("measured", value=round(engaged / float(reach.value) * 100, 1))


def _snapshots(
    target: PostTarget, answered: set[dt.datetime], now: dt.datetime
) -> tuple[list[dict[str, Any]], bool]:
    """The first-week rungs: captured, missed (past due and unanswered) or due."""
    published = target.published_at or now
    out: list[dict[str, Any]] = []
    missed = False
    for hours in LADDER_HOURS:
        rung = published + dt.timedelta(hours=hours)
        if rung in answered:
            state = "captured"
        elif rung + RUNG_TOLERANCE < now:
            state = "missed"
            missed = True
        else:
            state = "due"
        out.append(
            {"rung": LADDER_LABELS.get(int(hours), f"{int(hours)} h"), "at": rung, "state": state}
        )
    return out, missed


def _capture_state(row: _Row, missed: bool, now: dt.datetime) -> tuple[str, str]:
    account = row.account
    if account is not None and account.status in STALE_STATUSES:
        return "stale", "The account needs reconnecting: numbers are frozen at the last sync"
    if any(cell.state == "pending" for cell in row.cells.values()):
        return "pending", "Inside the platform's reporting delay, or before the first capture"
    if missed:
        return "partial", "A capture did not happen while this post was being measured"
    published = row.target.published_at or now
    if now - published < dt.timedelta(days=7):
        return "capturing", "Snapshots continue until day 7"
    return "complete", "Every first-week snapshot captured"


_PLATFORM_NAMES: dict[str, str] = {value: str(label) for value, label in Platform.choices}


def _platform_name(platform: str) -> str:
    return _PLATFORM_NAMES.get(platform, platform)


def _rows(
    workspace: Workspace, *, since: dt.datetime, until: dt.datetime, now: dt.datetime
) -> list[_Row]:
    targets = list(
        PostTarget.objects.filter(
            post__workspace=workspace,
            state=PostTargetState.PUBLISHED,
            published_at__gte=since,
            published_at__lte=until,
        )
        .select_related("post", "social_account")
        .order_by("-published_at", "-pk")[:POSTS_LIMIT]
    )
    if not targets:
        return []
    latest, answered = _latest_and_rungs(targets)
    from social_providers import registry

    selected = registry.selected_key()
    keys = (
        {selected}
        | {
            t.social_account.provider_key
            for t in targets
            if t.social_account and t.social_account.provider_key
        }
        | {row.provider_key for row in latest.values() if row.provider_key}
    )
    caps = _capabilities(keys)

    rows: list[_Row] = []
    for target in targets:
        account = target.social_account
        capture = latest.get(target.pk)
        provider = (
            (capture.provider_key if capture is not None else "")
            or (account.provider_key if account is not None else "")
            or selected
        )
        name = _platform_name(target.platform)
        row = _Row(target=target, account=account)
        for metric in METRICS:
            row.cells[metric] = _cell(
                metric=metric,
                target=target,
                account=account,
                capture=capture,
                capability=caps.get((provider, target.platform, metric)),
                now=now,
                platform_name=name,
            )
        row.snapshots, missed = _snapshots(target, answered.get(target.pk, set()), now)
        row.capture, row.capture_why = _capture_state(row, missed, now)
        rows.append(row)
    return rows


def _total_value(row: _Row, metric: str) -> tuple[int | None, str]:
    """`(value, flag)` — flag is "", "pending" or "unavailable"."""
    if metric == "engagement":
        parts = [row.cells[m] for m in ENGAGEMENT]
        measured = [int(c.value or 0) for c in parts if c.state == "measured"]
        if measured:
            return sum(measured), "pending" if any(c.state == "pending" for c in parts) else ""
        if any(c.state == "pending" for c in parts):
            return None, "pending"
        return None, "unavailable"
    cell = row.cells[metric]
    if cell.state == "measured":
        return int(cell.value or 0), ""
    return None, cell.state


def _account_label(account: SocialAccount | None, platform: str) -> str:
    if account is None:
        return _platform_name(platform)
    return account.handle or account.display_name or _platform_name(platform)


def _totals(rows: list[_Row], prev_rows: list[_Row] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    left_out = len(_accounts_in(rows) - _accounts_in(prev_rows)) if prev_rows is not None else 0
    for metric in TOTALS:
        total = 0
        counted: set[str] = set()
        unavailable: dict[str, str] = {}
        partial_days: set[dt.date] = set()
        pending_days: set[dt.date] = set()
        for row in rows:
            label = _account_label(row.account, row.target.platform)
            value, flag = _total_value(row, metric)
            day = timezone.localtime(row.target.published_at or timezone.now()).date()
            if value is not None:
                total += value
                counted.add(label)
            elif flag == "unavailable":
                unavailable.setdefault(
                    label,
                    row.cells[metric].why
                    if metric != "engagement"
                    else "No likes, comments, shares or saves reported",
                )
            if flag == "pending":
                pending_days.add(day)
            if row.capture in ("partial", "stale"):
                partial_days.add(day)
        # An account is "not included" only when it contributed nothing; one
        # post without a figure among several with one is a partial day.
        excluded = {label: why for label, why in unavailable.items() if label not in counted}

        change: dict[str, Any] | None = None
        if prev_rows is not None:
            cur, prev = _like_for_like(rows, prev_rows, metric)
            change = {
                "previous": prev,
                "current": cur,
                "percent": round((cur - prev) / prev * 100) if prev else None,
                "accounts_left_out": left_out,
            }
        out[metric] = {
            "total": total if counted else None,
            "accounts_counted": len(counted),
            "excluded": [{"account": k, "why": v} for k, v in sorted(excluded.items())],
            "partial_days": len(partial_days),
            "pending_days": len(pending_days),
            "change": change,
        }
    return out


def _accounts_in(rows: list[_Row]) -> set[str]:
    return {_account_label(r.account, r.target.platform) for r in rows}


def _like_for_like(rows: list[_Row], prev_rows: list[_Row], metric: str) -> tuple[int, int]:
    """Only accounts present in both periods — a newly connected account
    would otherwise read as growth."""
    both = _accounts_in(rows) & _accounts_in(prev_rows)

    def summed(group: list[_Row]) -> int:
        return sum(
            value
            for row in group
            if _account_label(row.account, row.target.platform) in both
            for value, _flag in [_total_value(row, metric)]
            if value is not None
        )

    return summed(rows), summed(prev_rows)


def _daily(rows: list[_Row], *, start: dt.date, days: int) -> dict[str, list[dict[str, Any]]]:
    by_day: dict[dt.date, list[_Row]] = defaultdict(list)
    for row in rows:
        if row.target.published_at:
            by_day[timezone.localtime(row.target.published_at).date()].append(row)
    out: dict[str, list[dict[str, Any]]] = {}
    for metric in TOTALS:
        series: list[dict[str, Any]] = []
        for offset in range(days):
            day = start + dt.timedelta(days=offset)
            by_platform: dict[str, int] = defaultdict(int)
            total: int | None = None
            partial: set[str] = set()
            pending: set[str] = set()
            for row in by_day.get(day, []):
                value, flag = _total_value(row, metric)
                name = _platform_name(row.target.platform)
                if value is not None:
                    by_platform[row.target.platform] += value
                    total = (total or 0) + value
                if flag == "pending":
                    pending.add(name)
                if row.capture in ("partial", "stale"):
                    partial.add(f"{_account_label(row.account, row.target.platform)} ({name})")
            series.append(
                {
                    "date": day,
                    "total": total,
                    "by_platform": dict(by_platform),
                    "partial_reasons": sorted(partial),
                    "pending_platforms": sorted(pending),
                    "posts": len(by_day.get(day, [])),
                }
            )
        out[metric] = series
    return out


def _accounts(workspace: Workspace, *, history: int, now: dt.datetime) -> list[dict[str, Any]]:
    """The connections page's cards, so both pages say the same thing about an
    account — plus how much of its history this plan shows."""
    from channels.overview import account_cards

    out: list[dict[str, Any]] = []
    for card in account_cards(workspace, now=now):
        account = card["account"]
        if account.status == SocialAccountStatus.REVOKED:
            continue
        held = max(0, (now - account.connected_at).days)
        out.append(
            {
                "id": account.pk,
                "platform": account.platform,
                "handle": account.handle,
                "display_name": account.display_name,
                "state": card["state"],
                "insights_off": card["insights_off"],
                "followers": card["followers"],
                "followers_change_7d": card["followers_change_7d"],
                "last_synced_at": card["last_synced_at"],
                "history_days_held": min(held, history) if history >= 0 else held,
            }
        )
    return out


def summary(
    workspace: Workspace, *, window: int | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    moment = now or timezone.now()
    days, history = resolve_window(workspace, window)
    today = timezone.localtime(moment).date()
    start_day = today - dt.timedelta(days=days - 1)
    since = timezone.make_aware(dt.datetime.combine(start_day, dt.time.min))

    ensure_within_horizon(workspace, starts_at=since, ends_at=moment)
    rows = _rows(workspace, since=since, until=moment, now=moment)

    # Like for like needs the previous window inside the plan's history too;
    # otherwise there is no comparison, and the page says why.
    prev_rows: list[_Row] | None = None
    if history < 0 or days * 2 <= history:
        prev_since = since - dt.timedelta(days=days)
        prev_rows = _rows(
            workspace, since=prev_since, until=since - dt.timedelta(microseconds=1), now=moment
        )

    posts = []
    for row in rows:
        target = row.target
        posts.append(
            {
                "target_id": target.pk,
                "post_id": target.post_id,
                "title": (target.post.master_body or "").strip()[:120],
                "platform": target.platform,
                "account": _account_label(row.account, target.platform),
                "format": target.post_format or "",
                "published_at": target.published_at,
                "cells": {metric: cell.as_dict() for metric, cell in row.cells.items()},
                "engagement_rate": _engagement_rate(row.cells).as_dict(),
                "capture": row.capture,
                "capture_why": row.capture_why,
                "snapshots": row.snapshots,
            }
        )

    return {
        "window": days,
        "default_window": default_window(history),
        "windows": [{"days": w, "locked": history >= 0 and w > history} for w in WINDOWS],
        "history_days": history,
        "start": start_day,
        "end": today,
        "comparable": prev_rows is not None,
        "totals": _totals(rows, prev_rows),
        "daily": _daily(rows, start=start_day, days=days),
        "accounts": _accounts(workspace, history=history, now=moment),
        "posts": posts,
        "posts_truncated": len(rows) >= POSTS_LIMIT,
    }
