"""Competitor tracking (P6-08).

**A source kind, not a second pipeline.** BUILD-PLAN says it outright: this
overlaps the trend engine's tracked-accounts source, so it is built as
`TrendSourceKind.COMPETITOR` and runs the same stages — ingest, normalise,
score — rather than a parallel set of its own. What that buys, concretely:

*Scoring is already partitioned.* Stage 3 percentile-normalises within source
kind, so a competitor's engagement is ranked against other competitors and
never against a Reddit thread with no follower count. A parallel pipeline would
have had to reinvent that, and would eventually have reinvented it differently.

*Exclusion is already modelled.* `excluded_reason` and the language filter apply
unchanged, so a competitor's non-English post drops out for the same reason and
by the same code as anything else.

**The one thing that is different is tenancy.** Every source before this one was
category-shared, which is what makes the trend engine cheap. A competitor list
is not shareable: who a brand watches is competitive information about that
brand. So a competitor source carries a `workspace`, the shared corpus filters
those out (`ingest.sources_for`), and extraction here runs over one workspace's
sources alone.

**That is also why stage 4 is skipped.** `TrendCluster` is keyed on
`(category, platform)` and is the shared corpus every workspace in a category
reads; clustering a private item into it would put one brand's competitor list
on every other brand's trend page. Ranking within the kind is what this surface
needs, and stage 3 already provides it — see `refresh`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import QuerySet
from rest_framework.exceptions import ValidationError

from trends.models import TrendItem, TrendSource, TrendSourceKind
from trends.services import ingest, normalize, scoring

if TYPE_CHECKING:
    from workspaces.models import Workspace

logger = logging.getLogger(__name__)

#: The same window the shared corpus uses. One number, one meaning — a
#: competitor comparison over a different window than the trend view beside it
#: would invite exactly the comparison it cannot support.
WINDOW_DAYS = 14


def _normalised_handle(handle: str) -> str:
    cleaned = handle.strip().lstrip("@")
    if not cleaned:
        raise ValidationError({"handle": "A competitor is identified by an account handle."})
    return cleaned.lower()


def tracked(workspace: Workspace, *, platform: str = "") -> QuerySet[TrendSource]:
    """This workspace's competitor sources. Never anybody else's — the filter
    is the tenancy boundary, so it lives here rather than in a caller."""
    rows = TrendSource.objects.filter(
        workspace=workspace, kind=TrendSourceKind.COMPETITOR, is_active=True
    )
    return rows.filter(platform=platform) if platform else rows


@transaction.atomic
def track(workspace: Workspace, *, platform: str, handle: str, label: str = "") -> TrendSource:
    """Start watching one competitor on one platform.

    Capped by the plan, because how many competitors a workspace may watch is a
    commercial number and every commercial number is an admin-editable row
    (Part 7 rule 10). Exhaustion is a 402 with an upgrade path, from the one
    entitlement resolver — never a check written here.
    """
    from billing.services.entitlements import entitlements_for

    if workspace.category_id is None:
        # The source hangs off a category like every other one, and the
        # comparison reads within it. A workspace that has not finished
        # onboarding has no category to put a competitor in — refused here
        # rather than left to a null constraint further down.
        raise ValidationError({"category": "Finish onboarding before tracking competitors."})

    cleaned = _normalised_handle(handle)
    existing: TrendSource | None = (
        tracked(workspace, platform=platform).filter(handle=cleaned).first()
    )
    if existing is not None:
        return existing

    entitlements_for(workspace).check_quota("max_tracked_competitors", tracked(workspace).count())

    source, _created = TrendSource.objects.update_or_create(
        workspace=workspace,
        platform=platform,
        kind=TrendSourceKind.COMPETITOR,
        handle=cleaned,
        defaults={
            "category_id": workspace.category_id,
            "label": label or handle.strip(),
            "query": {"handle": cleaned, "platform": platform},
            "is_active": True,
        },
    )
    return source


def untrack(source: TrendSource) -> TrendSource:
    """Deactivated, not deleted.

    The items already ingested are what a past comparison was computed from,
    and deleting the source would take them with it — turning "we stopped
    watching them in March" into "we never watched them".
    """
    if source.is_active:
        source.is_active = False
        source.save(update_fields=["is_active", "updated_at"])
    return source


def refresh(workspace: Workspace, *, platform: str) -> list[TrendItem]:
    """Run stages 1 to 3 over this workspace's competitors on one platform.

    **Stage 4 is deliberately not run.** `TrendCluster` is keyed on
    `(category, platform)` and is the shared corpus every workspace in a
    category reads; clustering private items into it would put one brand's
    competitor list on every other brand's trend page. Ranking within the kind
    is what this surface needs, and stage 3 already provides it.
    """
    sources = list(tracked(workspace, platform=platform))
    if not sources:
        return []

    ingest.ingest_sources(sources)
    window = ingest.window_items(sources, days=WINDOW_DAYS)
    kept = normalize.normalize(window)
    scored = scoring.score(kept)
    logger.info(
        "refreshed competitors",
        extra={
            "workspace_id": workspace.pk,
            "platform": platform,
            "sources": len(sources),
            "kept": len(kept),
        },
    )
    return scored


def _engagement_rate(interactions: float, followers: int) -> float | None:
    """Interactions over audience, or `None` when there is no audience to
    divide by.

    **Not zero.** An account whose follower count the vendor did not report is
    unmeasured, and a zero here would read as "they get no engagement" — the
    exact confusion Part 7 rule 12 exists to prevent, applied to somebody
    else's account.
    """
    if followers <= 0:
        return None
    return round(interactions / followers, 6)


def _top_post(items: list[TrendItem]) -> dict[str, Any] | None:
    from common.ranking import INTERACTION_WEIGHTS

    def weighted(item: TrendItem) -> float:
        return sum(
            weight * float(item.raw_metrics.get(key, 0) or 0)
            for key, weight in INTERACTION_WEIGHTS.items()
        )

    best = max(items, key=weighted, default=None)
    if best is None:
        return None
    return {
        "body": best.body,
        "posted_at": best.posted_at,
        "url": best.media_url,
        "interactions": weighted(best),
    }


def comparison(workspace: Workspace, *, platform: str, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """This workspace against the competitors it tracks, on one platform.

    **Both sides are computed the same way**, from interactions over audience,
    so the two numbers beside each other are actually comparable. The
    alternative — our impressions against their likes — is the kind of chart
    that reads as insight and means nothing.

    Our side reads `PostMetric.analysable()`, so a fabricated row cannot enter
    a comparison a customer acts on (Part 7 rule 17), and an unavailable
    capture is excluded rather than counted as zero.

    A competitor with nothing in the window is reported with `posts: 0` and a
    **null** rate, not a zero one: we watched and they published nothing, which
    is a fact worth showing and is not the same as engagement of zero.
    """
    import datetime as dt

    from django.db.models import Max, Sum
    from django.utils import timezone

    from analytics.models import PostMetric
    from common.ranking import INTERACTION_WEIGHTS

    since = timezone.now() - dt.timedelta(days=days)

    ours = PostMetric.objects.analysable().filter(
        post_target__post__workspace=workspace,
        post_target__platform=platform,
        captured_at__gte=since,
    )
    # Newest capture per target: totals are cumulative, so summing every rung
    # would count the same post five times and flatter us against a competitor
    # measured once.
    newest = {
        (row["post_target"], row["newest"])
        for row in ours.values("post_target").annotate(newest=Max("captured_at"))
    }
    latest_ids = [
        row["id"]
        for row in ours.values("id", "post_target", "captured_at")
        if (row["post_target"], row["captured_at"]) in newest
    ]
    our_rows = PostMetric.objects.filter(id__in=latest_ids).aggregate(
        **{key: Sum(key) for key in INTERACTION_WEIGHTS}
    )
    our_interactions = sum(
        weight * float(our_rows.get(key) or 0) for key, weight in INTERACTION_WEIGHTS.items()
    )
    our_followers = sum(
        account.followers_cached or 0
        for account in workspace.social_accounts.filter(platform=platform)
    )

    competitors = []
    for source in tracked(workspace, platform=platform).order_by("label", "handle"):
        items = list(
            TrendItem.objects.filter(source=source, posted_at__gte=since, excluded_reason="")
        )
        interactions = sum(
            weight * float(item.raw_metrics.get(key, 0) or 0)
            for item in items
            for key, weight in INTERACTION_WEIGHTS.items()
        )
        followers = max((item.author_followers for item in items), default=0)
        competitors.append(
            {
                "handle": source.handle,
                "label": source.label or source.handle,
                "posts": len(items),
                "followers": followers or None,
                "interactions": interactions,
                "engagement_rate": _engagement_rate(interactions, followers),
                # Their best post in the window, by the same weighted
                # interaction count the rate above uses — a "top post" chosen
                # by a different measure than the column beside it is how a
                # comparison stops being one.
                "top_post": _top_post(items),
            }
        )

    return {
        "platform": platform,
        "window_days": days,
        "us": {
            "posts": len(latest_ids),
            "followers": our_followers or None,
            "interactions": our_interactions,
            "engagement_rate": _engagement_rate(our_interactions, our_followers),
        },
        "competitors": competitors,
    }
