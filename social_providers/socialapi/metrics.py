"""Measurement through SocialAPI (SA-50, SA-52, SA-53).

**One call per rung, not a delta feed.** SocialAPI has no change feed (plan
G2). The batch post listing was meant to stand in for one, but it carries
Instagram's placeholder `shares: 0` / `saves: 0` and no insight figures, so a
rung filled from it would be worse than a rung filled from
`GET /posts/{pid}/metrics`, which returns the counts *and* the insights for a
post in a single call. The ladder (T+1h … weekly to day 30, at most nine reads
per post in its life) decides when to ask, on the background share of the team
budget (C-06), so the cost is bounded by posts, not by polling. This provider
therefore has no `changed_since`, and `ingest.follow_delta` passes it by.

**What it refuses to call a number** — plan §5.3, every rule in code here or in
`analytics.services.normalise`:

1. A pair the capability table marks unavailable is null whatever arrived
   (`supports` reads the admin-editable `MetricCapability` rows).
2. A target the vendor has never synced (`metrics_synced_at` null) writes no
   numbers: *pending* while the platform may still report, *unavailable* after.
3. A metric the vendor omitted is null; and on an account whose insights
   permission is missing, the insight figures are null even when the vendor
   sends a `0` for them.
4. Account history keeps the vendor's gauge/flow kind, `days_spanned` and
   `subjects_counted` (`analytics.services.account_history` reads them).
5. Our metric names map to the vendor's through a table, never a branch.

The raw response is kept as the payload body (C-07b), wrapped with what we
observed when we read it, so a normalisation fix is a reprocess.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from analytics.providers.base import METRIC_KEYS, RawMetricPayload, proportions
from common.timestamps import parse_or_none
from social_providers.models import CallKind
from social_providers.socialapi.budget import Priority
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.errors import SocialAPIError, to_metrics_error

PROVIDER_KEY = "socialapi"

#: Bumped when the body's shape changes in a way that alters how it is read.
#: Stored on every row, so a reprocess knows which reading wrote it.
SCHEMA_VERSION = 1

#: Our metric → the vendor's field on a target's `metrics` (rule 5). `None`:
#: the vendor has no field that means this, so it is never read.
METRIC_FIELDS: dict[str, str | None] = {
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saves": "saves",
    "reach": "reach",
    "views": "views",
    # Instagram's profile website taps; "clicks" on a link is a different
    # question the vendor does not answer, and the capability table says so.
    "clicks": "website_clicks",
    "impressions": None,
}

#: Figures that exist only with the platform's insights permission. Their
#: presence proves the permission; on an account without it they are null
#: even when a `0` arrives (rule 3).
INSIGHT_FIELDS = frozenset({"reach", "views", "interactions", "shares", "saves"})
#: The subset whose *absence*, past the reporting delay, proves the permission
#: is missing. Shares and saves are left out: the vendor's own listing carries
#: them as zeros either way.
INSIGHT_PROOF_FIELDS = frozenset({"reach", "views", "interactions"})

#: How long an unsynced target counts as pending rather than unavailable, at
#: least. The vendor syncs "periodically" without saying how often; a day is
#: long enough that a rung is not written off before the first sync, and short
#: enough that a platform which never reports stops being asked.
SYNC_GRACE = dt.timedelta(hours=24)

#: Our platform → the vendor's slug, where they differ.
VENDOR_SLUGS: dict[str, str] = {"google_business": "google"}

#: Platform → (summary path, {our stat: vendor field}). Declared, not
#: branched: a platform missing here reports its counts as unknown.
SUMMARIES: dict[str, tuple[str, dict[str, str]]] = {
    "instagram": (
        "/platforms/instagram/accounts/{id}/summary",
        {
            "followers": "follower_count",
            "following": "following_count",
            "total_posts": "media_count",
        },
    ),
    "youtube": (
        "/platforms/youtube/accounts/{id}/summary",
        {"followers": "subscriber_count", "total_posts": "video_count"},
    ),
    "tiktok": (
        "/platforms/tiktok/accounts/{id}/summary",
        {
            "followers": "follower_count",
            "following": "following_count",
            "total_posts": "video_count",
        },
    ),
    "pinterest": (
        "/platforms/pinterest/accounts/{id}/summary",
        {"followers": "follower_count", "total_posts": "pin_count"},
    ),
}

#: Platforms with an audience breakdown, and our dimension → the vendor's.
DEMOGRAPHICS: dict[str, dict[str, str]] = {
    "instagram": {"AGE": "age", "GENDER": "gender", "COUNTRY": "country", "CITY": "city"},
}

#: Platforms whose daily performance report comes from their own route rather
#: than the account history (Google's search and maps views, calls, …).
PERFORMANCE_ROUTES: dict[str, str] = {
    "google_business": "/platforms/google/accounts/{id}/insights",
}

#: The vendor code for an account connected before the insights permission.
_SCOPE_MISSING = "insights_scope_missing"


@dataclass(frozen=True)
class AccountDay:
    """One point of an account's daily history, as the vendor reported it."""

    metric: str
    day: dt.date
    kind: str  # "gauge" (a level, diffed) or "flow" (a per-day count, summed)
    value: int | None
    change: int | None
    days_spanned: int
    subjects_counted: int
    day_tz: str
    source: str


class SocialAPIMetricsProvider:
    key = PROVIDER_KEY
    schema_version = SCHEMA_VERSION

    def __init__(self, client: SocialAPIClient | None = None) -> None:
        self.client = client or SocialAPIClient()
        self._available: set[tuple[str, str]] | None = None
        self._latency: dict[str, dt.timedelta] = {}

    # --- capability --------------------------------------------------------------------
    def _load_capabilities(self) -> set[tuple[str, str]]:
        """The admin-editable truth table, read once per instance — a flip in
        admin (SA-90's evidence) takes effect on the next capture."""
        if self._available is None:
            from analytics.models import MetricCapability

            rows = MetricCapability.objects.filter(provider_key=self.key).values_list(
                "platform", "metric_key", "available", "latency_hint"
            )
            self._available = set()
            for platform, metric, available, latency in rows:
                if available:
                    self._available.add((platform, metric))
                    if latency and latency > self._latency.get(platform, dt.timedelta(0)):
                        self._latency[platform] = latency
        return self._available

    def supports(self, platform: str, metric: str) -> bool:
        return (platform, metric) in self._load_capabilities()

    def _reporting_delay(self, platform: str) -> dt.timedelta:
        self._load_capabilities()
        return self._latency.get(platform, dt.timedelta(0))

    def _expects_insights(self, platform: str) -> bool:
        return self.supports(platform, "reach")

    # --- one post ----------------------------------------------------------------------
    def fetch(self, *, platform: str, provider_post_id: str) -> RawMetricPayload:
        """Counts and insights for one published post, in one call."""
        now = timezone.now()
        if not any(self.supports(platform, metric) for metric in METRIC_KEYS):
            # Nothing on this platform is readable: no call, and no zeros.
            return self._payload(platform, provider_post_id, "UNAVAILABLE", {}, now)
        try:
            answer = self.client.get(
                f"/posts/{provider_post_id}/metrics", priority=Priority.BACKGROUND
            ).data
        except SocialAPIError as error:
            if error.kind in (CallKind.NOT_FOUND, CallKind.UNSUPPORTED):
                return self._payload(platform, provider_post_id, "UNAVAILABLE", {}, now)
            raise to_metrics_error(error) from error

        response = answer if isinstance(answer, dict) else {}
        body = {"response": response, "observed": {"platform": platform, "at": now.isoformat()}}
        target = _target(body)
        if target is None:
            return self._payload(platform, provider_post_id, "UNAVAILABLE", body, now)

        error_code = str((target.get("error") or {}).get("code") or "")
        if error_code == "resource.not_supported":
            return self._payload(platform, provider_post_id, "UNAVAILABLE", body, now)
        if _synced_at(target) is None or error_code:
            # Rule 2: never synced is not "nothing happened". Pending while the
            # platform may still report; a permanent gap after.
            published = parse_or_none(target.get("published_at"))
            waited = now - published if published else dt.timedelta(0)
            pending = waited < max(SYNC_GRACE, self._reporting_delay(platform))
            return self._payload(
                platform, provider_post_id, "PENDING" if pending else "UNAVAILABLE", body, now
            )
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            body=body,
            availability="MEASURED",
            fetched_at=now,
            insights_permission=self.insights_permission(body),
        )

    def insights_permission(self, body: dict[str, Any]) -> bool | None:
        """What this answer proves about the account's insights permission
        (SA-53): `True` when an insight figure came back, `False` when none
        did on a post old enough for the platform to have reported, `None`
        when it proves nothing (a platform without insights, or too soon)."""
        observed = body.get("observed") or {}
        platform = str(observed.get("platform") or "")
        target = _target(body)
        if target is None or not self._expects_insights(platform):
            return None
        metrics = target.get("metrics") or {}
        if any(metrics.get(name) is not None for name in INSIGHT_PROOF_FIELDS):
            return True
        published = parse_or_none(target.get("published_at"))
        at = parse_or_none(observed.get("at"))
        if published is None or at is None or at - published < self._reporting_delay(platform):
            return None
        return False

    def read_metric(self, body: Any, metric: str) -> int | None:
        """One of our metrics out of a stored body, or `None` (rules 3 and 5)."""
        field_name = METRIC_FIELDS.get(metric)
        target = _target(body) if isinstance(body, dict) else None
        if field_name is None or target is None:
            return None
        platform = str((body.get("observed") or {}).get("platform") or "")
        if (
            field_name in INSIGHT_FIELDS
            and self._expects_insights(platform)
            and self.insights_permission(body) is not True
        ):
            # Rule 3: until an insight figure proves the permission, a `0`
            # here may be the permission missing, not the audience.
            return None
        return _count((target.get("metrics") or {}).get(field_name))

    def _payload(
        self,
        platform: str,
        provider_post_id: str,
        availability: str,
        body: dict[str, Any],
        now: dt.datetime,
    ) -> RawMetricPayload:
        return RawMetricPayload(
            provider_key=self.key,
            platform=platform,
            provider_post_id=provider_post_id,
            schema_version=self.schema_version,
            body=body,
            availability=availability,
            fetched_at=now,
        )

    # --- one account -------------------------------------------------------------------
    def fetch_account_stats(
        self, *, provider_account_id: str, platform: str = ""
    ) -> dict[str, Any]:
        """Follower, following and post counts, `None` where the platform's
        summary does not carry them. `insights_permission` is `False` when
        Instagram refused for want of the permission (SA-53)."""
        stats: dict[str, Any] = {"followers": None, "following": None, "total_posts": None}
        route = SUMMARIES.get(platform)
        if route is None:
            return stats
        path, fields = route
        try:
            answer = self.client.get(path.format(id=provider_account_id)).data
        except SocialAPIError as error:
            if _scope_missing(error):
                return {**stats, "insights_permission": False}
            raise to_metrics_error(error) from error
        summary = _unwrap(answer)
        for ours, theirs in fields.items():
            stats[ours] = _count(summary.get(theirs))
        if platform in DEMOGRAPHICS:
            stats["insights_permission"] = True
        return stats

    def fetch_demographics(
        self, *, platform: str, provider_account_id: str
    ) -> dict[str, Any] | None:
        """Shares per bucket for each dimension the platform breaks down, or
        `None`: no breakdown on this platform, fewer than 100 followers (the
        vendor answers an empty list), or no insights permission."""
        dimensions = DEMOGRAPHICS.get(platform)
        if dimensions is None:
            return None
        slug = VENDOR_SLUGS.get(platform, platform)
        breakdowns: dict[str, Any] = {}
        for ours, theirs in dimensions.items():
            try:
                answer = self.client.get(
                    f"/platforms/{slug}/accounts/{provider_account_id}/demographics",
                    params={"breakdown": theirs, "timeframe": "last_30_days"},
                ).data
            except SocialAPIError as error:
                if _scope_missing(error):
                    return None
                raise to_metrics_error(error) from error
            entries = _unwrap(answer).get("entries") or []
            shares = proportions(
                {
                    str(entry.get("value")): entry.get("count")
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("value") is not None
                }
            )
            if shares:
                breakdowns[ours] = shares
        return breakdowns or None

    def fetch_account_history(
        self, *, platform: str, provider_account_id: str, start: dt.date, end: dt.date
    ) -> list[AccountDay]:
        """The account's daily history, with the vendor's own kind, gap and
        coverage markers kept (rule 4); plus a platform performance report
        where one exists (Google's)."""
        days: list[AccountDay] = []
        try:
            answer = self.client.get(
                f"/accounts/{provider_account_id}/analytics",
                params={"from": start.isoformat(), "to": end.isoformat()},
            ).data
        except SocialAPIError as error:
            if error.kind not in (CallKind.NOT_FOUND, CallKind.UNSUPPORTED):
                raise to_metrics_error(error) from error
            answer = {}
        for series in _unwrap(answer).get("series") or []:
            if not isinstance(series, dict) or not series.get("metric"):
                continue
            kind = "gauge" if series.get("kind") == "gauge" else "flow"
            for point in series.get("points") or []:
                day = _day(point.get("day")) if isinstance(point, dict) else None
                if day is None:
                    continue
                days.append(
                    AccountDay(
                        metric=str(series["metric"])[:64],
                        day=day,
                        kind=kind,
                        value=_signed(point.get("value")),
                        change=_signed(point.get("change")),
                        days_spanned=max(_signed(point.get("days_spanned")) or 1, 1),
                        subjects_counted=max(_signed(point.get("subjects_counted")) or 1, 1),
                        day_tz=str(series.get("day_tz") or "")[:32],
                        source=str(series.get("source") or "")[:16],
                    )
                )
        route = PERFORMANCE_ROUTES.get(platform)
        if route is not None:
            days += self._performance(route, provider_account_id, start, end)
        return days

    def _performance(
        self, route: str, provider_account_id: str, start: dt.date, end: dt.date
    ) -> list[AccountDay]:
        try:
            answer = self.client.get(
                route.format(id=provider_account_id),
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            ).data
        except SocialAPIError as error:
            if error.kind in (CallKind.NOT_FOUND, CallKind.UNSUPPORTED):
                return []
            raise to_metrics_error(error) from error
        days = []
        for series in _unwrap(answer).get("metrics") or []:
            if not isinstance(series, dict) or not series.get("metric"):
                continue
            for point in series.get("points") or []:
                day = _day(point.get("date")) if isinstance(point, dict) else None
                if day is None:
                    continue
                days.append(
                    AccountDay(
                        metric=str(series["metric"])[:64],
                        day=day,
                        kind="flow",
                        value=_signed(point.get("value")),
                        change=None,
                        days_spanned=1,
                        subjects_counted=1,
                        day_tz="",
                        source="reported",
                    )
                )
        return days


# --- reading the vendor's answers ---------------------------------------------------------
def _unwrap(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        return {}
    inner = answer.get("data")
    return inner if isinstance(inner, dict) else answer


def _target(body: Any) -> dict[str, Any] | None:
    """The one target of the stored answer (one vendor post per target)."""
    response = body.get("response") if isinstance(body, dict) else None
    targets = _unwrap(response).get("targets") or []
    first = targets[0] if targets else None
    return first if isinstance(first, dict) else None


def _synced_at(target: dict[str, Any]) -> dt.datetime | None:
    metrics = target.get("metrics") or {}
    return parse_or_none(target.get("metrics_synced_at") or metrics.get("metrics_synced_at"))


def _scope_missing(error: SocialAPIError) -> bool:
    return error.vendor_code.endswith(_SCOPE_MISSING) or error.kind == CallKind.SCOPE_MISSING


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _signed(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
