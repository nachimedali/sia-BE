"""Stage 4 — cluster (design.md §8.4): pgvector embeddings, rolling window.

**Single-pass greedy agglomeration against running centroids**, not k-means.
k-means needs `k` chosen up front, and nothing about a category's week tells you
how many distinct things are being said in it — the honest answer is "as many as
clear the similarity bar". Greedy assignment gives that, is deterministic given
a stable input order (stage 3 hands the window over sorted by composite score,
so the strongest item seeds each cluster), and is O(items * clusters) on a
window of tens, which is the size D11's on-demand model produces.

The centroid is stored so a later extraction can attach a new item to an
existing cluster without re-clustering the whole window — the same reason
`TrendCluster.centroid` is a column rather than a derived value.
"""

from __future__ import annotations

import logging
import math

from django.utils import timezone

from ai.providers.embeddings import get_embedding_provider
from common.text import content_tokens
from trends.models import TrendCluster, TrendItem

logger = logging.getLogger(__name__)

#: Cosine similarity above which an item joins a cluster rather than seeding a
#: new one. Tuned against the fixture corpus: reworded posts about the same idea
#: land near 0.8, genuinely different subjects well below 0.5.
SIMILARITY_THRESHOLD = 0.62

#: A cluster of one is an observation, not a pattern. Singletons are dropped
#: rather than shown — design.md §8.4's whole synthesis rule rests on a cluster
#: describing several sources, and the UI puts `item_count` on the card as
#: evidence, where "1" would be an anti-recommendation.
MIN_CLUSTER_SIZE = 2


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _mean(vectors: list[list[float]]) -> list[float]:
    count = len(vectors)
    return [sum(values) / count for values in zip(*vectors, strict=True)]


def _label(items: list[TrendItem], *, words: int = 4) -> str:
    """The cluster's name, from the vocabulary its members actually share.

    Deliberately extractive rather than an LLM call: this runs on every
    extraction for every cluster, a generated label would cost a request each,
    and the words the members have in common are a more honest description of
    what grouped them than a paraphrase would be.

    Ties break on where the word falls in the strongest member's own sentence,
    not alphabetically. In a small cluster almost every shared word is tied at
    the same count, so alphabetical order would pick by spelling — turning
    "unboxing · ceramic · mug" into "ceramic · hand · light", which describes
    the props instead of the pattern.
    """
    lead = max(items, key=lambda item: item.composite_score)
    lead_order = {token: index for index, token in enumerate(content_tokens(lead.body))}

    counts: dict[str, int] = {}
    for item in items:
        for token in dict.fromkeys(content_tokens(item.body)):
            counts[token] = counts.get(token, 0) + 1

    shared = sorted(
        counts.items(),
        key=lambda pair: (-pair[1], lead_order.get(pair[0], len(lead_order))),
    )[:words]
    if not shared:
        return "Untitled pattern"
    return " · ".join(word for word, _count in shared)


def embed_items(items: list[TrendItem]) -> list[TrendItem]:
    """Fills in any missing embedding, in one batched provider call.

    Items already carrying a vector are skipped — re-extraction of a window that
    mostly overlaps the last one should not pay to re-embed what has not changed.
    """
    pending = [item for item in items if item.embedding is None]
    if pending:
        vectors = get_embedding_provider().embed([item.body for item in pending])
        for item, vector in zip(pending, vectors, strict=True):
            item.embedding = vector
        TrendItem.objects.bulk_update(pending, ["embedding"])
    return [item for item in items if item.embedding is not None]


def cluster_window(
    *, category_id: int, platform: str, items: list[TrendItem], window_days: int
) -> list[TrendCluster]:
    """Groups a scored window into clusters and replaces the category's previous
    ones.

    Replacement rather than merge: a cluster is a statement about a window, and
    leaving the last window's clusters alongside this one's would double-count
    the same items under two labels.
    """
    embedded = embed_items(items)
    now = timezone.now()

    groups: list[list[TrendItem]] = []
    centroids: list[list[float]] = []
    for item in embedded:
        vector = list(item.embedding or [])
        best_index, best_score = -1, SIMILARITY_THRESHOLD
        for index, centroid in enumerate(centroids):
            similarity = _cosine(vector, centroid)
            if similarity >= best_score:
                best_index, best_score = index, similarity

        if best_index < 0:
            groups.append([item])
            centroids.append(vector)
        else:
            groups[best_index].append(item)
            centroids[best_index] = _mean([list(m.embedding or []) for m in groups[best_index]])

    window_start = min((item.posted_at for item in embedded), default=now)
    TrendCluster.objects.filter(category_id=category_id, platform=platform).delete()

    clusters: list[TrendCluster] = []
    for group, centroid in zip(groups, centroids, strict=True):
        if len(group) < MIN_CLUSTER_SIZE:
            continue
        cluster = TrendCluster.objects.create(
            category_id=category_id,
            platform=platform,
            label=_label(group),
            centroid=centroid,
            window_start=window_start,
            window_end=now,
            item_count=len(group),
            # A cluster inherits the mean of its members: how hard this idea is
            # pulling, not how hard its single best example is.
            velocity_score=sum(m.velocity_score for m in group) / len(group),
            recency_score=sum(m.recency_score for m in group) / len(group),
            composite_score=sum(m.composite_score for m in group) / len(group),
            expires_at=TrendCluster.default_expiry(now),
        )
        TrendItem.objects.filter(pk__in=[m.pk for m in group]).update(cluster=cluster)
        clusters.append(cluster)

    logger.info(
        "clustered trend window",
        extra={
            "category_id": category_id,
            "platform": platform,
            "items": len(embedded),
            "clusters": len(clusters),
            "window_days": window_days,
        },
    )
    return sorted(clusters, key=lambda cluster: cluster.composite_score, reverse=True)
