"""Which of the six tools can run right now, and why the others cannot (X-08).

The tools board rendered all six identically whatever the workspace had, so
pressing "Hashtag ranker" with an empty corpus returned an empty list and no
explanation — charged nothing, said nothing. Three of the six read data rather
than a model (`_hashtags` the category corpus, `_best_time` this workspace's
own captures, `_thread` nothing at all), so "ready" is a per-tool question with
four different answers.

**The reasons are keys, not sentences.** `no_corpus` and `no_captures` travel
to a client that owns the wording, the illustration and where the button goes
— the same split `ToolConfigSerializer` already makes for the display names.

Every check here mirrors the runner it guards, and mirrors it by *calling the
same thing where there is one* — `signals.performance` for best-time, the
corpus query for hashtags. A readiness check that drifts from its runner is
worse than none: it promises an answer the tool then refuses to give.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db.models import Count
from django.utils import timezone

from billing.services.entitlements import entitlements_for
from common.setup import Requirement, done, is_ready, missing
from tools.models import Tool, ToolConfig, ToolUsage
from tools.services import HASHTAG_WINDOW_DAYS
from workspaces.models import Workspace

READY = "ready"
BLOCKED = "blocked"

#: Which tools stop working when a given prerequisite is missing. The three
#: writers are absent on purpose: a topic typed into the form is all they need.
NEEDS_CORPUS = frozenset({Tool.HASHTAG})
NEEDS_CAPTURES = frozenset({Tool.BEST_TIME})


def _corpus_items(workspace: Workspace) -> int:
    from integrations.trendfeed import get_trend_feed

    if workspace.category_id is None:
        return 0
    since = timezone.now() - dt.timedelta(days=HASHTAG_WINDOW_DAYS)
    return get_trend_feed(workspace.organization).corpus_size(
        category_id=int(workspace.category_id), since=since
    )


def _published_targets(workspace: Workspace) -> int:
    """What `_best_time` will read, read the same way — `signals.performance`
    is the one place the plan's history horizon is applied, and a second
    bounded query here would eventually disagree with it."""
    from analytics.services import signals

    horizon = entitlements_for(workspace).analytics_horizon_days()
    return len(signals.performance(workspace.pk, horizon_days=horizon))


def tool_requirements(
    workspace: Workspace, *, balance: int, corpus: int, captures: int
) -> list[Requirement]:
    """`credits` blocks everything; the other two block one tool each, so they
    are warnings rather than closed doors — five tools still work."""
    cheapest = ToolConfig.objects.order_by("credits_cost").values_list("credits_cost", flat=True)
    price = next(iter(cheapest), 1)

    credits_row = (done if balance >= price else missing)(
        "credits", f"{balance} credits", facts={"balance": balance, "cheapest": price}
    )
    corpus_row = (done if corpus else missing)(
        "trend_corpus",
        f"{corpus} items in the last {HASHTAG_WINDOW_DAYS} days",
        blocking=False,
        facts={
            "items": corpus,
            "window_days": HASHTAG_WINDOW_DAYS,
            "has_category": workspace.category_id is not None,
        },
    )
    captures_row = (done if captures else missing)(
        "captures",
        f"{captures} published posts measured",
        blocking=False,
        facts={"targets": captures},
    )
    return [credits_row, corpus_row, captures_row]


def _reason(
    config: ToolConfig, *, funded: bool, has_category: bool, corpus: int, captures: int
) -> str | None:
    if not config.is_enabled:
        return "disabled"
    if not funded:
        return "no_credits"
    if config.slug in NEEDS_CORPUS:
        if not has_category:
            return "no_category"
        if not corpus:
            return "no_corpus"
    if config.slug in NEEDS_CAPTURES and not captures:
        return "no_captures"
    return None


def _used_this_month(workspace: Workspace) -> dict[str, int]:
    """Runs per tool since the first of this month (UTC), this workspace only."""
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        ToolUsage.objects.filter(workspace=workspace, created_at__gte=start)
        .values("tool")
        .annotate(n=Count("id"))
    )
    return {row["tool"]: row["n"] for row in rows}


def tool_states(
    workspace: Workspace, *, balance: int, corpus: int, captures: int
) -> list[dict[str, Any]]:
    used = _used_this_month(workspace)

    states = []
    for config in ToolConfig.objects.all():
        reason = _reason(
            config,
            funded=balance >= config.credits_cost,
            has_category=workspace.category_id is not None,
            corpus=corpus,
            captures=captures,
        )
        states.append(
            {
                "slug": config.slug,
                "status": BLOCKED if reason else READY,
                "reason": reason or "",
                "credits_cost": config.credits_cost,
                "used_this_month": used.get(config.slug, 0),
            }
        )
    return states


def tools_readiness(workspace: Workspace) -> dict[str, Any]:
    # Each of these is a query (or several); both halves of the answer read the
    # same three numbers, so they are taken once.
    balance = entitlements_for(workspace).credits_remaining()
    corpus = _corpus_items(workspace)
    captures = _published_targets(workspace)
    requirements = tool_requirements(workspace, balance=balance, corpus=corpus, captures=captures)
    return {
        "ready": is_ready(requirements),
        "balance": balance,
        "requirements": requirements,
        "tools": tool_states(workspace, balance=balance, corpus=corpus, captures=captures),
    }
