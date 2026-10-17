"""Collection: ask each due source, upsert what it found, rescore the wall.

**Due-based, not a sweep.** Beat calls `collect_due` often; each source is
asked only when `last_run_at + refresh_minutes` has passed, so a YouTube search
(100 quota units) runs a few times a day while the 1-unit chart and the free
Google feed refresh more often — the cadence is the row's, not the schedule's.

**One dead provider never fails the run.** A `CollectorError` is recorded on
its own source (`consecutive_failures`, `last_error`), the third in a row is
logged as an error, and every other source still collects. The wall keeps
serving what it had: a refused read is not an incident.

**Idempotent.** The post's identity is `(region, provider, external_id)`, so a
re-read refreshes the numbers it already had rather than inserting a copy.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.utils import timezone

from playbook.collectors.base import CollectedPost, CollectorError
from playbook.collectors.registry import get_collector
from playbook.models import PlaybookPost, PlaybookSource
from playbook.services import scoring

logger = logging.getLogger(__name__)

ALERT_AFTER_FAILURES = 3


def due_sources(now: dt.datetime | None = None) -> list[PlaybookSource]:
    """Sources are a handful of rows per market, so the due test reads better
    in Python than as interval arithmetic in SQL."""
    now = now or timezone.now()
    return [
        source
        for source in PlaybookSource.objects.filter(is_active=True).select_related("category")
        if source.last_run_at is None
        or source.last_run_at + dt.timedelta(minutes=source.refresh_minutes) <= now
    ]


def _upsert(source: PlaybookSource, post: CollectedPost, now: dt.datetime) -> None:
    fields: dict[str, Any] = {
        "source": source,
        "format": post.format,
        "url": post.url,
        "thumbnail_url": post.thumbnail_url,
        "thumbnail_credit": post.thumbnail_credit,
        "title": post.title,
        "caption": post.caption,
        "creator": post.creator,
        "hashtags": post.hashtags,
        "lang": post.lang,
        "duration_seconds": post.duration_seconds,
        "posted_at": post.posted_at,
        "views": post.views,
        "likes": post.likes,
        "comments": post.comments,
        "traffic_floor": post.traffic_floor,
        "traffic_label": post.traffic_label,
        "news": post.news,
        "payload": post.payload,
        "is_fake": post.is_fake,
        "last_seen_at": now,
    }
    existing = PlaybookPost.objects.filter(
        region=source.region, provider=source.provider, external_id=post.external_id
    ).first()
    if existing is None:
        PlaybookPost.objects.create(
            region=source.region,
            provider=source.provider,
            external_id=post.external_id,
            category=source.category,
            **fields,
        )
        return
    for name, value in fields.items():
        setattr(existing, name, value)
    # A category's search knows what a video is about; the market chart does
    # not, so a later chart sighting never erases a category.
    if source.category_id is not None:
        existing.category = source.category
    existing.save()


def collect_source(source: PlaybookSource, *, now: dt.datetime | None = None) -> int:
    now = now or timezone.now()
    collector = get_collector(source.provider)
    source.last_run_at = now
    try:
        found: list[CollectedPost] = []
        for request in collector.plan(source):
            found.extend(collector.fetch(request))
    except CollectorError as error:
        source.consecutive_failures += 1
        source.last_error = str(error)[:300]
        source.save(update_fields=["last_run_at", "consecutive_failures", "last_error"])
        log = (
            logger.error if source.consecutive_failures >= ALERT_AFTER_FAILURES else logger.warning
        )
        log(
            "playbook source failed",
            extra={"source_id": source.pk, "failures": source.consecutive_failures},
        )
        return 0

    for post in found:
        _upsert(source, post, now)
    source.last_success_at = now
    source.consecutive_failures = 0
    source.last_error = ""
    source.save(
        update_fields=["last_run_at", "last_success_at", "consecutive_failures", "last_error"]
    )
    return len(found)


def collect_due(*, now: dt.datetime | None = None) -> dict[str, int]:
    """Every due source, then one rescore per market that changed."""
    now = now or timezone.now()
    collected = 0
    regions: set[str] = set()
    sources = due_sources(now)
    for source in sources:
        collected += collect_source(source, now=now)
        regions.add(source.region)
    for region in sorted(regions):
        scoring.rescore(region, now=now)
    return {"sources": len(sources), "posts": collected}
