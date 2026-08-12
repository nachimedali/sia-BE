"""Stage 5 — expose (design.md §8.4, D11): rank with decay, cache, serve.

This is the only entry point the rest of the system uses. Everything above it
is a stage; this is the policy — *when* to run them.

**On demand, cached for 30 days, with a manual refresh** (D11). Continuous
harvest across every vertical is uneconomic, so the first workspace in a
category to open `/app/trends` pays for the extraction and every later one is
served from what it produced. `TrendCluster.is_fresh` resolves against the clock
at read time, so a stale corpus is discovered by the read that needs it rather
than by a sweep that has to have run first — the same shape `Entitlements`'s
trial lapse and `Reminder`'s token TTL already use.
"""

from __future__ import annotations

import logging

from categories.models import Category
from trends.models import TrendCluster
from trends.services import clustering, ingest, normalize, scoring

logger = logging.getLogger(__name__)

#: The rolling window every extraction looks back over. Two weeks: long enough
#: that a quiet category still has enough items to cluster, short enough that
#: "what is working now" is still true.
WINDOW_DAYS = 14


def cached_clusters(category_id: int, platform: str) -> list[TrendCluster]:
    """What is servable right now, freshest-ranked. Empty when nothing has been
    extracted yet or everything has expired.

    `.defer("centroid")` because nothing downstream of this function reads it —
    `top_cluster` wants a label, the API serializers never list it — so there is
    no reason to deserialize a 1536-float vector into Python on every read.
    """
    return [
        cluster
        for cluster in TrendCluster.objects.filter(
            category_id=category_id, platform=platform
        )
        .defer("centroid")
        .order_by("-composite_score")
        if cluster.is_fresh
    ]


def extract(*, category_id: int, platform: str, force: bool = False) -> list[TrendCluster]:
    """Runs the pipeline for one `(category, platform)`, or serves the cache.

    `force` is the manual refresh (D11) — it re-runs even when the cached
    corpus is still fresh, which is what the "refresh" action on `/app/trends`
    and the `extract_recipes --force` shape both need.
    """
    if not force:
        cached = cached_clusters(category_id, platform)
        if cached:
            return cached

    sources = ingest.sources_for(Category.objects.get(pk=category_id), platform)
    ingest.ingest_sources(sources)
    window = ingest.window_items(sources, days=WINDOW_DAYS)
    kept = normalize.normalize(window)
    scored = scoring.score(kept)
    clusters = clustering.cluster_window(
        category_id=category_id, platform=platform, items=scored, window_days=WINDOW_DAYS
    )

    logger.info(
        "extracted trends",
        extra={
            "category_id": category_id,
            "platform": platform,
            "window": len(window),
            "kept": len(kept),
            "clusters": len(clusters),
            "forced": force,
        },
    )
    return clusters


def top_cluster(category_id: int, platform: str) -> TrendCluster | None:
    """The single strongest fresh cluster — what grounds a prompt (design.md
    §8.3). Never triggers an extraction: generation must not block on a vendor,
    and an ungrounded prompt is a worse outcome than a slow one only if the
    grounding actually arrives."""
    return next(iter(cached_clusters(category_id, platform)), None)
