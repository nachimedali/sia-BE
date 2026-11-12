"""What each template did for this brand (S17-37, BUILD-PLAN L-7).

Per template: the posts sent from its renders, how many were measured, and
their median engagement — **computed in code** (rule 5) from this workspace's
own captures, through `PostMetric.objects.analysable()`: measured and real,
never an unavailable reading and never the test fake (rules 4 and 17). A post
counts once, at the mean of its targets' latest measured rates.

Below `ENOUGH` measured posts a result is shown but marked not enough to go on:
BUILD-PLAN Phase 7's "insufficient" floor, and the reason a recommendation is
only ever lifted by a template with enough of them.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Any

from ai.models import VideoRender
from analytics.models import PostMetric
from workspaces.models import Workspace

#: Measured posts below which a median is not enough to go on (Phase 7, n < 8).
ENOUGH = 8


def results_for(workspace: Workspace) -> dict[str, dict[str, Any]]:
    """`{template: {posts, measured, median_engagement, enough}}` for every
    template this workspace has sent a post from."""
    by_post = dict(
        VideoRender.objects.filter(workspace=workspace, post__isnull=False)
        .exclude(template="")
        .values_list("post_id", "template")
    )
    if not by_post:
        return {}
    latest: dict[int, tuple[Any, float]] = {}
    post_of: dict[int, int] = {}
    for target_id, post_id, captured_at, rate in (
        PostMetric.objects.analysable()
        .filter(post_target__post_id__in=list(by_post))
        .values_list("post_target_id", "post_target__post_id", "captured_at", "engagement_rate")
    ):
        if rate is None:
            continue
        post_of[target_id] = post_id
        if target_id not in latest or captured_at > latest[target_id][0]:
            latest[target_id] = (captured_at, float(rate))
    rates_by_post: dict[int, list[float]] = {}
    for target_id, (_when, rate) in latest.items():
        rates_by_post.setdefault(post_of[target_id], []).append(rate)

    out: dict[str, dict[str, Any]] = {}
    for post_id, template in by_post.items():
        row = out.setdefault(template, {"posts": 0, "rates": []})
        row["posts"] += 1
        if post_id in rates_by_post:
            row["rates"].append(mean(rates_by_post[post_id]))
    return {
        template: {
            "posts": row["posts"],
            "measured": len(row["rates"]),
            "median_engagement": round(median(row["rates"]), 4) if row["rates"] else None,
            "enough": len(row["rates"]) >= ENOUGH,
        }
        for template, row in out.items()
    }
