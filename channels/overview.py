"""What the connections page shows about each account, and what each platform shares.

Read-only, computed from rows we already keep — no provider call is made to
draw the page:

* **state** — the account's lifecycle in the words the page uses
  (`connected`, `syncing` before its first capture, `reconnect`, `removed`,
  `over`, `disconnected`);
* **numbers** — followers from the latest account snapshot, the 7-day change
  only when a snapshot from a week ago exists (otherwise null: "no sync this
  week", never 0), the last time anything was captured, and what depends on
  the account: posts scheduled through it, posts awaiting approval for its
  platform, posts published through it in 30 days;
* **the pooled cap** — accounts count against the organization (L-1), so the
  meter shows every workspace's share;
* **what a platform shares** — the selected provider's capability rows, the
  same rows normalisation obeys, so the promise made before connecting is the
  behaviour after.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db.models import Count, Max, Q
from django.utils import timezone

from analytics.models import AccountSnapshot, MetricCapability, PostMetric
from billing.services.entitlements import entitlements_for
from channels.models import SocialAccount, SocialAccountStatus
from channels.services import QUOTA_KEY, pooled_accounts
from content.models import Platform, Post, PostStatus, PostTarget, PostTargetState
from workspaces.models import Workspace

STATE_BY_STATUS: dict[str, str] = {
    SocialAccountStatus.ACTIVE: "connected",
    SocialAccountStatus.REAUTH_REQUIRED: "reconnect",
    SocialAccountStatus.PAGE_REMOVED: "removed",
    SocialAccountStatus.OVER_LIMIT: "over",
    SocialAccountStatus.REVOKED: "disconnected",
}


def followers(
    accounts: list[SocialAccount], now: dt.datetime
) -> dict[int, tuple[int | None, int | None]]:
    """Per account: followers from its latest snapshot, and the change since a
    snapshot taken 7 to 9 days ago — null without one ("no sync this week"),
    never 0. Two queries for any number of accounts."""
    ids = [account.pk for account in accounts]
    readings = AccountSnapshot.objects.filter(
        social_account_id__in=ids, followers__isnull=False
    ).order_by("social_account_id", "-captured_at")

    def first_per_account(rows: Any) -> dict[int, int]:
        pairs = rows.distinct("social_account_id").values_list("social_account_id", "followers")
        return {pk: count for pk, count in pairs if count is not None}

    latest = first_per_account(readings)
    week_ago = first_per_account(
        readings.filter(
            captured_at__lte=now - dt.timedelta(days=7),
            captured_at__gte=now - dt.timedelta(days=9),
        )
    )
    out: dict[int, tuple[int | None, int | None]] = {}
    for account in accounts:
        if account.pk not in latest:
            out[account.pk] = (account.followers_cached or None, None)
            continue
        current = latest[account.pk]
        before = week_ago.get(account.pk)
        out[account.pk] = (current, None if before is None else current - before)
    return out


def account_cards(workspace: Workspace, *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    now = now or timezone.now()
    accounts = list(
        SocialAccount.objects.filter(workspace=workspace).order_by("connected_at", "pk")
    )
    if not accounts:
        return []
    ids = [account.pk for account in accounts]

    target_stats = {
        row["social_account"]: row
        for row in PostTarget.objects.filter(social_account_id__in=ids)
        .values("social_account")
        .annotate(
            scheduled=Count(
                "id",
                filter=Q(state=PostTargetState.PENDING, post__status=PostStatus.SCHEDULED),
            ),
            published_30d=Count(
                "id",
                filter=Q(
                    state=PostTargetState.PUBLISHED,
                    published_at__gte=now - dt.timedelta(days=30),
                ),
            ),
        )
    }
    last_metric = dict(
        PostMetric.objects.filter(post_target__social_account_id__in=ids)
        .values("post_target__social_account")
        .annotate(at=Max("captured_at"))
        .values_list("post_target__social_account", "at")
    )
    last_snapshot = dict(
        AccountSnapshot.objects.filter(social_account_id__in=ids)
        .values("social_account")
        .annotate(at=Max("captured_at"))
        .values_list("social_account", "at")
    )
    awaiting = {
        platform: Post.objects.filter(
            workspace=workspace,
            status=PostStatus.PENDING_REVIEW,
            planned_platforms__contains=[platform],
        ).count()
        for platform in {account.platform for account in accounts}
    }

    follower_counts = followers(accounts, now)
    cards: list[dict[str, Any]] = []
    for account in accounts:
        synced = [at for at in (last_metric.get(account.pk), last_snapshot.get(account.pk)) if at]
        last_synced_at = max(synced) if synced else None
        state = STATE_BY_STATUS.get(str(account.status), "connected")
        if state == "connected" and last_synced_at is None:
            state = "syncing"
        count, change = follower_counts[account.pk]
        stats: dict[str, Any] = dict(target_stats.get(account.pk) or {})
        cards.append(
            {
                "account": account,
                "state": state,
                "insights_off": account.capabilities.get("insights") is False,
                "followers": count,
                "followers_change_7d": change,
                "last_synced_at": last_synced_at,
                "scheduled": stats.get("scheduled", 0),
                "awaiting_approval": awaiting.get(account.platform, 0),
                "published_30d": stats.get("published_30d", 0),
            }
        )
    return cards


def cap_summary(workspace: Workspace) -> dict[str, Any]:
    """The organization's pooled account cap, and each workspace's share."""
    limit = entitlements_for(workspace).quota(QUOTA_KEY)
    pooled = pooled_accounts(workspace)
    by_workspace = list(
        pooled.values("workspace_id", "workspace__name").annotate(n=Count("id")).order_by("-n")
    )
    over = SocialAccount.objects.filter(
        workspace__organization_id=workspace.organization_id,
        status=SocialAccountStatus.OVER_LIMIT,
    ).count()
    return {
        "limit": None if limit is None or limit < 0 else int(limit),
        "used": sum(row["n"] for row in by_workspace),
        "over": over,
        "by_workspace": [
            {
                "name": row["workspace__name"],
                "count": row["n"],
                "current": row["workspace_id"] == workspace.pk,
            }
            for row in by_workspace
        ],
    }


def platform_shares() -> dict[str, Any]:
    """Per platform: each shown metric (available, delay, why not), whether
    comments can be read, what is needed on the platform's side, and whether
    connecting it pauses for a choice. From the selected provider's rows and
    what its connect adapter declares."""
    from analytics.services.summary import METRICS
    from social_providers import registry
    from social_providers.socialapi.capabilities import REQUIREMENTS

    provider = registry.selected_key()
    adapter = registry.port_class("connect")
    unsupported: dict[str, str] = getattr(adapter, "unsupported", {})
    rows: dict[tuple[str, str], MetricCapability] = {
        (row.platform, row.metric_key): row
        for row in MetricCapability.objects.filter(provider_key=provider)
    }
    platforms: dict[str, Any] = {}
    for platform in Platform.values:
        metrics: dict[str, Any] = {}
        for metric in METRICS:
            row = rows.get((platform, metric))
            metrics[metric] = {
                # No row is undeclared, not available: say so rather than promise.
                "available": bool(row.available) if row else False,
                "delay_hours": row.delay_hours if row else 0,
                "note": (row.note if row else "Not declared for this provider yet"),
            }
        platforms[platform] = {
            "metrics": metrics,
            "comments": platform in getattr(adapter, "comment_platforms", ()),
            "requires": REQUIREMENTS.get(platform, ""),
            "connectable": platform not in unsupported,
            "unavailable_reason": unsupported.get(platform, ""),
            "selection": platform in getattr(adapter, "selection_platforms", ()),
        }
    return {"provider": provider, "platforms": platforms}
