"""Stage 1 — ingest (design.md §8.4).

Per-source adapters, watermarks, upsert on `UNIQUE(source, external_id)`.

**Idempotent by construction.** The same run twice writes the same rows: the
unique pair is the identity, so a re-fetch updates the metrics it already had
rather than inserting a second copy. That is what makes the on-demand refresh
(D11) safe to press twice, and what `test_trend_ingest_is_idempotent_on_
external_id` holds in place.

Metrics are refreshed on re-ingest but the pipeline's own columns are not:
`excluded_reason`, the scores, the embedding and the cluster all belong to
later stages, and clobbering them here would mean a refresh silently
un-rejected everything stage 2 had already thrown out.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from categories.models import Category
from trends.models import TrendItem, TrendSource
from trends.providers.base import TrendVendorError
from trends.providers.vendors import get_trend_vendor

logger = logging.getLogger(__name__)

#: Per source, per run. Enough to see a week of a busy category without asking
#: a vendor for a page count nobody reads.
FETCH_LIMIT = 100


def ingest_source(source: TrendSource) -> int:
    """Fetches everything new from one source and returns how many rows it
    touched. A vendor failure is logged and swallowed: `trends_q` is lowest
    priority and "the last corpus stays valid" (design.md §5.1), so one dead
    source must not fail an extraction the other sources can still serve."""
    try:
        raw_items = get_trend_vendor(source.kind).fetch(
            query=source.query, since=source.watermark_at, limit=FETCH_LIMIT
        )
    except TrendVendorError:
        logger.warning(
            "trend source unavailable", exc_info=True, extra={"source_id": source.pk}
        )
        return 0

    newest: dt.datetime | None = source.watermark_at
    for raw in raw_items:
        TrendItem.objects.update_or_create(
            source=source,
            external_id=raw.external_id,
            defaults={
                "modality": raw.modality,
                "author_handle": raw.author_handle,
                "author_followers": raw.author_followers,
                "body": raw.body,
                "media_url": raw.media_url,
                "posted_at": raw.posted_at,
                "raw_metrics": raw.raw_metrics,
                "lang": raw.lang,
            },
        )
        if newest is None or raw.posted_at > newest:
            newest = raw.posted_at

    if newest is not None and newest != source.watermark_at:
        source.watermark_at = newest
        source.save(update_fields=["watermark_at", "updated_at"])

    return len(raw_items)


def sources_for(category: Category, platform: str) -> list[TrendSource]:
    """The most specific active sources that cover this category.

    Sources are seeded on root categories (`seed_trend_sources`), but a
    workspace's category is usually a leaf — "Homeware & Ceramics", not "Home &
    Lifestyle". Rather than duplicate every vendor query down the tree, a
    category with no sources of its own inherits its nearest ancestor's. Most
    specific wins: giving one leaf its own hand-tuned source in admin overrides
    the inherited set for that leaf alone.
    """
    for node in [category, *reversed(category.ancestors())]:
        sources = list(
            TrendSource.objects.filter(category=node, platform=platform, is_active=True)
        )
        if sources:
            return sources
    return []


def ingest_sources(sources: list[TrendSource]) -> int:
    return sum(ingest_source(source) for source in sources)


def window_items(sources: list[TrendSource], *, days: int) -> list[TrendItem]:
    """The rolling window every later stage works over.

    Scoped to the same resolved source list ingest just filled, not to a
    category id — with inheritance those are different things, and querying by
    category would find nothing for every leaf that borrows its root's sources.
    """
    if not sources:
        return []
    since = timezone.now() - dt.timedelta(days=days)
    return list(
        TrendItem.objects.filter(
            source__in=sources, posted_at__gte=since
        ).select_related("source")
    )
