"""The provider cockpit (SA-80, `social-provider-plan.md` §9).

The operator's view of the whole SocialAPI relationship, in one read: is the
vendor reachable and what is left of the plan, which accounts need a person,
what is publishing or stuck, whether webhooks are arriving, what capture is
measuring and what it cannot, what the audience costs, and who is waiting on
the rate budget. **Staff only and not tenant-scoped** — it is the operator's
view across every workspace, never shown to a customer.

Every panel carries `alerts`: the sentences that make it red. Nothing here
writes; the two vendor reads (`/usage`, `/webhooks`) are free and cached
briefly, so a refreshing page does not spend the budget.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from typing import Any

from django.core.cache import cache
from django.db.models import Count, Max, Q
from django.utils import timezone

from analytics.models import (
    AccountSnapshot,
    AudienceComment,
    AudienceCommentAction,
    Availability,
    CaptureDeferral,
    CommentAction,
    MetricCapability,
    PostMetric,
)
from analytics.providers.base import METRIC_KEYS
from channels.models import SocialAccount, SocialAccountStatus
from content.models import Post, PostStatus, PostTarget, PostTargetState, ProviderState
from social_providers import registry
from social_providers.models import CallKind, DeliveryStatus, ProviderCall, WebhookDelivery
from social_providers.socialapi.connect import rows

PROVIDER = "socialapi"

#: A publish accepted this long ago and still unconfirmed is stuck.
STUCK_AFTER = dt.timedelta(minutes=10)
#: Share of a vendor allowance that turns its panel red.
ALLOWANCE_WARNING = 0.8
#: Share of failed publishes over the week that turns publishing red.
FAILURE_RATE_WARNING = 0.05
#: How long the free vendor reads are reused between refreshes.
VENDOR_CACHE_SECONDS = 60


def cockpit(*, now: dt.datetime | None = None) -> dict[str, Any]:
    moment = now or timezone.now()
    usage = _vendor("usage", "/usage")
    return {
        "generated_at": moment,
        "provider": provider_panel(moment, usage),
        "accounts": accounts_panel(),
        "publishing": publishing_panel(moment),
        "webhooks": webhooks_panel(moment),
        "capture": capture_panel(moment),
        "audience": audience_panel(moment, usage),
        "rate_budget": rate_budget_panel(moment),
    }


# --- the vendor's free reads --------------------------------------------------------------
def _vendor(name: str, path: str) -> dict[str, Any]:
    """`{"data": …}` or `{"error": …}` — a vendor outage is shown, not raised."""
    key = f"socialapi:cockpit:{name}"
    cached = cache.get(key)
    if cached is not None:
        return dict(cached)
    if registry.selected_key() != PROVIDER or not registry.selected().has_credential:
        return {"error": "SocialAPI is not the selected provider, or has no key."}
    from social_providers.socialapi.client import SocialAPIClient
    from social_providers.socialapi.errors import SocialAPIError

    try:
        answer = SocialAPIClient().get(path).data
    except SocialAPIError as error:
        return {"error": f"{error.message} (request {error.request_id or '-'})"}
    result = {"data": answer}
    cache.set(key, result, VENDOR_CACHE_SECONDS)
    return result


def _share(used: Any, limit: Any) -> float | None:
    if not isinstance(used, int) or not isinstance(limit, int) or limit <= 0:
        return None
    return used / limit


# --- panels -------------------------------------------------------------------------------
def provider_panel(now: dt.datetime, usage: dict[str, Any]) -> dict[str, Any]:
    alerts: list[str] = []
    hour = ProviderCall.objects.filter(
        provider=PROVIDER, created_at__gte=now - dt.timedelta(hours=1)
    )
    day = ProviderCall.objects.filter(provider=PROVIDER, created_at__gte=now - dt.timedelta(days=1))

    latencies = sorted(hour.filter(local=False).values_list("latency_ms", flat=True)[:2000])
    errors = [
        {"kind": kind, "code": code, "count": count, "last_request_id": last}
        for kind, code, count, last in _error_rows(day)
    ]
    if day.filter(kind=CallKind.MISCONFIGURED).exists():
        alerts.append("Our key or setup was refused in the last 24 h.")
    if hour.filter(kind=CallKind.RATE_LIMITED, local=False).exists():
        alerts.append("The vendor rate-limited us in the last hour.")

    plan = usage.get("data") if isinstance(usage.get("data"), dict) else None
    if plan:
        for label, used, limit in (
            ("posts", plan.get("posts_used"), plan.get("posts_limit")),
            ("interactions", plan.get("interactions_used"), plan.get("interactions_limit")),
        ):
            share = _share(used, limit)
            if share is not None and share >= ALLOWANCE_WARNING:
                alerts.append(f"{used} of {limit} {label} used this period.")
    elif usage.get("error"):
        alerts.append(f"Usage unreadable: {usage['error']}")

    seen = (
        ProviderCall.objects.filter(provider=PROVIDER, rate_limit__isnull=False)
        .order_by("-created_at")
        .values("rate_limit", "rate_remaining", "created_at")
        .first()
    )
    return {
        "status": registry.status(),
        "usage": plan,
        "usage_error": usage.get("error", ""),
        "rate_limit_seen": seen,
        "calls_last_hour": dict(Counter(hour.values_list("call_class", flat=True))),
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
        "errors_24h": errors,
        "alerts": alerts,
    }


def _error_rows(calls: Any) -> list[tuple[str, str, int, str]]:
    failed = calls.exclude(kind=CallKind.OK)
    grouped = (
        failed.values("kind", "error_code").annotate(count=Count("id")).order_by("-count")[:20]
    )
    rows = []
    for group in grouped:
        last = (
            failed.filter(kind=group["kind"], error_code=group["error_code"])
            .order_by("-created_at")
            .values_list("request_id", flat=True)
            .first()
        )
        rows.append((group["kind"], group["error_code"], group["count"], last or ""))
    return rows


def _percentile(values: list[int], share: float) -> int | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return int(statistics.quantiles(values, n=100, method="inclusive")[int(share * 100) - 1])


def accounts_panel() -> dict[str, Any]:
    accounts = list(
        SocialAccount.objects.filter(provider_key=PROVIDER)
        .exclude(status=SocialAccountStatus.REVOKED)
        .select_related("workspace")
        .order_by("workspace__name", "platform")
    )
    last_read = dict(
        AccountSnapshot.objects.filter(social_account__in=accounts)
        .values("social_account")
        .annotate(at=Max("captured_at"))
        .values_list("social_account", "at")
    )
    attention = {SocialAccountStatus.REAUTH_REQUIRED, SocialAccountStatus.PAGE_REMOVED}
    rows = [
        {
            "id": account.pk,
            "workspace": account.workspace.name,
            "platform": account.platform,
            "handle": account.handle or account.display_name,
            "status": account.status,
            "insights_off": (account.capabilities or {}).get("insights") is False,
            "reconnect_reason": account.reconnect_reason,
            "last_read_at": last_read.get(account.pk),
        }
        for account in accounts
    ]
    needing = [row for row in rows if row["status"] in attention]
    alerts = [
        f"{row['workspace']}: {row['platform']} {row['handle']} needs reconnecting."
        for row in needing
    ]
    return {"accounts": rows, "alerts": alerts}


def publishing_panel(now: dt.datetime) -> dict[str, Any]:
    ours = PostTarget.objects.filter(provider_key=PROVIDER)
    in_flight = ours.filter(
        state=PostTargetState.PUBLISHING, provider_state=ProviderState.PUBLISH_REQUESTED
    )
    stuck = list(
        in_flight.filter(provider_state_at__lte=now - STUCK_AFTER)
        .select_related("post__workspace")
        .order_by("provider_state_at")[:20]
    )
    week = ours.filter(updated_at__gte=now - dt.timedelta(days=7))
    failed = week.filter(state=PostTargetState.FAILED)
    published = week.filter(state=PostTargetState.PUBLISHED).count()
    failures = Counter(
        str((detail or {}).get("code") or "unknown")
        for detail in failed.values_list("error_detail", flat=True)
    )
    failed_count = sum(failures.values())
    rate = failed_count / (failed_count + published) if failed_count + published else None

    alerts = []
    if stuck:
        alerts.append(f"{len(stuck)} publish(es) unconfirmed for over {STUCK_AFTER}.")
    if rate is not None and rate > FAILURE_RATE_WARNING:
        alerts.append(f"{rate:.0%} of this week's publishes failed.")
    return {
        "scheduled_next_24h": Post.objects.filter(
            status=PostStatus.SCHEDULED,
            scheduled_at__gte=now,
            scheduled_at__lte=now + dt.timedelta(hours=24),
        ).count(),
        "in_flight": in_flight.count(),
        "stuck": [
            {
                "target_id": target.pk,
                "workspace": target.post.workspace.name,
                "platform": target.platform,
                "provider_post_id": target.provider_post_id,
                "since": target.provider_state_at,
            }
            for target in stuck
        ],
        "published_7d": published,
        "failed_7d": failed_count,
        "failures_by_code_7d": dict(failures.most_common(10)),
        "drafts_open": ours.filter(provider_state=ProviderState.DRAFT_CREATED).count(),
        "alerts": alerts,
    }


def webhooks_panel(now: dt.datetime) -> dict[str, Any]:
    from django.conf import settings

    from social_providers.socialapi.webhooks import counts

    day = WebhookDelivery.objects.filter(
        provider=PROVIDER, received_at__gte=now - dt.timedelta(days=1)
    )
    last = WebhookDelivery.objects.filter(provider=PROVIDER).aggregate(at=Max("received_at"))["at"]
    refused = counts(now.date())
    endpoints = _vendor("webhooks", "/webhooks")
    configured = bool(getattr(settings, "SOCIALAPI_WEBHOOK_SECRET", ""))
    published_today = PostTarget.objects.filter(
        provider_key=PROVIDER, published_at__gte=now - dt.timedelta(days=1)
    ).exists()

    alerts = []
    if not configured:
        alerts.append("No webhook secret: every event arrives by polling.")
    elif published_today and not day.exists():
        alerts.append("Posts were published in the last 24 h but no webhook arrived.")
    if refused["refused_signature"]:
        alerts.append(f"{refused['refused_signature']} delivery(ies) failed the signature today.")
    if day.filter(status=DeliveryStatus.FAILED).exists():
        alerts.append("Some deliveries failed processing in the last 24 h.")
    return {
        "secret_configured": configured,
        "endpoints": (
            [row for row in rows(endpoints["data"]) if row.get("id")]
            if "data" in endpoints
            else None
        ),
        "endpoints_error": endpoints.get("error", ""),
        "last_received_at": last,
        "by_event_24h": dict(day.values_list("event").annotate(n=Count("id"))),
        "by_status_24h": dict(day.values_list("status").annotate(n=Count("id"))),
        "refused_today": refused,
        "alerts": alerts,
    }


def _coverage(captures: Any) -> dict[str, dict[str, Any]]:
    """platform → `{"rows": n, metric: captures that carried a figure, …}`."""
    counts = {
        f"has_{metric}": Count("id", filter=Q(**{f"{metric}__isnull": False}))
        for metric in METRIC_KEYS
    }
    out: dict[str, dict[str, Any]] = {}
    for row in captures.values("post_target__platform").annotate(rows=Count("id"), **counts):
        data: dict[str, Any] = dict(row)
        out[str(data["post_target__platform"])] = {
            "rows": data["rows"],
            **{metric: data[f"has_{metric}"] for metric in METRIC_KEYS},
        }
    return out


def capture_panel(now: dt.datetime) -> dict[str, Any]:
    """What capture measured over the week, per platform and metric, and
    whether a pair the capability table promises has produced only nulls."""
    week = PostMetric.objects.filter(
        provider_key=PROVIDER, captured_at__gte=now - dt.timedelta(days=7)
    )
    by_availability = dict(week.values_list("availability").annotate(n=Count("id")))
    measured = week.filter(availability=Availability.MEASURED)
    coverage = {
        platform: {metric: {"rows": row["rows"], "measured": row[metric]} for metric in METRIC_KEYS}
        for platform, row in _coverage(measured).items()
    }
    recent = _coverage(measured.filter(captured_at__gte=now - dt.timedelta(hours=48)))
    alerts = []
    for platform, metric in MetricCapability.objects.filter(
        provider_key=PROVIDER, available=True
    ).values_list("platform", "metric_key"):
        row = recent.get(platform)
        if row and row["rows"] and metric in METRIC_KEYS and not row[metric]:
            alerts.append(f"{platform} {metric} is marked available but measured nothing in 48 h.")
    return {
        "by_availability_7d": by_availability,
        "coverage_7d": coverage,
        "deferred_rungs": CaptureDeferral.objects.count(),
        "alerts": alerts,
    }


def audience_panel(now: dt.datetime, usage: dict[str, Any]) -> dict[str, Any]:
    day = now - dt.timedelta(days=1)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    data = usage.get("data")
    plan: dict[str, Any] = data if isinstance(data, dict) else {}
    used, limit = plan.get("interactions_used"), plan.get("interactions_limit")
    share = _share(used, limit)
    alerts = []
    if share is not None and share >= ALLOWANCE_WARNING:
        alerts.append(f"{used} of {limit} provider interactions used this period.")
    return {
        "comments_24h": AudienceComment.objects.measured().filter(created_at__gte=day).count(),
        "replies_this_month": AudienceCommentAction.objects.filter(
            action=CommentAction.REPLY, created_at__gte=month
        ).count(),
        "moderation_this_month": AudienceCommentAction.objects.filter(created_at__gte=month)
        .exclude(action=CommentAction.REPLY)
        .count(),
        "provider_interactions": {"used": used, "limit": limit},
        "alerts": alerts,
    }


def rate_budget_panel(now: dt.datetime) -> dict[str, Any]:
    from django.conf import settings

    from social_providers.socialapi.budget import Priority, TeamBudget

    hour = ProviderCall.objects.filter(
        provider=PROVIDER, created_at__gte=now - dt.timedelta(hours=1), local=True
    )
    refused = dict(hour.values_list("priority").annotate(n=Count("id")))
    alerts = []
    if refused.get(Priority.PUBLISH):
        alerts.append("A publish waited on the rate budget in the last hour.")
    return {
        "limit_per_minute": TeamBudget().limit(),
        "publish_reserve_ratio": float(settings.SOCIALAPI_PUBLISH_RESERVE_RATIO),
        "refused_last_hour": refused,
        "capture_deferrals": CaptureDeferral.objects.count(),
        "alerts": alerts,
    }
