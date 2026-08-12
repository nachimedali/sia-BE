"""Trend corpus (design.md §6.6, §8.4; implementation.md Phase 10).

Three tables for the three things the pipeline produces: where to look
(`TrendSource`), what was found (`TrendItem`), and what it adds up to
(`TrendCluster`). Extraction is on demand per `(category, platform)` and cached
to `expires_at` (D11) — continuous harvest across every vertical is uneconomic,
and a 30-day-old craft pattern is still a craft pattern.

`TrendItem` carries three things design.md §6.6's field list does not name, each
because a pipeline stage has nowhere else to put its output: the per-item scores
stage 3 ranks on, the `excluded_reason` stage 2 writes instead of deleting, and
the `cluster`/`embedding` pair stage 4 needs to group on. Every one of them is
derived and recomputable from the raw columns above it.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone
from pgvector.django import VectorField

from content.models import Platform

#: `text-embedding-3-small`'s width, and the fake's. A `vector` column is fixed
#: at one dimension in Postgres, so this is a schema fact, not a setting: an
#: embedding model of a different width needs a migration, which is the honest
#: signal that every stored vector has to be recomputed.
EMBEDDING_DIMENSIONS = 1536

#: How long an extracted corpus stays servable (D11).
CACHE_TTL_DAYS = 30


class TrendSourceKind(models.TextChoices):
    ADLIB = "ADLIB", "Meta Ad Library"
    CREATIVE_CENTER = "CREATIVE_CENTER", "TikTok Creative Center"
    YOUTUBE = "YOUTUBE", "YouTube"
    REDDIT = "REDDIT", "Reddit"
    RSS = "RSS", "RSS feed"
    ACCOUNT = "ACCOUNT", "Tracked account"


class TrendModality(models.TextChoices):
    TEXT = "TEXT", "Text"
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class TrendSource(models.Model):
    """One place to look, for one category on one platform.

    `vendor` is who is asked rather than what is asked for (D12: RapidAPI or
    equivalent for Ad Library and Creative Center, YouTube and Reddit direct) —
    it selects the adapter, so a dead vendor is a row edit, not a deploy.
    """

    category = models.ForeignKey(
        "categories.Category", on_delete=models.CASCADE, related_name="trend_sources"
    )
    platform = models.CharField(max_length=32, choices=Platform.choices)
    kind = models.CharField(max_length=24, choices=TrendSourceKind.choices)
    vendor = models.CharField(max_length=60, blank=True)
    query = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    #: The newest `posted_at` this source has already yielded. Ingest asks only
    #: for what came after it, so a re-run costs one request rather than the
    #: whole history — and an empty watermark is a first run, not an error.
    watermark_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["category__name", "platform", "kind"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["category", "platform", "kind", "vendor"],
                name="unique_trend_source_per_category_platform_kind_vendor",
            )
        ]

    def __str__(self) -> str:
        return f"{self.category_id}/{self.platform}/{self.kind}"


class TrendItem(models.Model):
    source = models.ForeignKey(TrendSource, on_delete=models.CASCADE, related_name="items")
    external_id = models.CharField(max_length=200)
    modality = models.CharField(
        max_length=8, choices=TrendModality.choices, default=TrendModality.TEXT
    )
    author_handle = models.CharField(max_length=200, blank=True)
    #: Typed rather than left in `raw_metrics` because `authority` is computed
    #: from it on every scoring pass, and arithmetic over a JSON lookup is both
    #: slower and easier to get silently wrong when a vendor omits the key.
    author_followers = models.PositiveIntegerField(default=0)
    body = models.TextField(blank=True)
    media_url = models.URLField(max_length=500, blank=True)
    posted_at = models.DateTimeField()
    raw_metrics = models.JSONField(default=dict, blank=True)
    lang = models.CharField(max_length=8, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    #: Stage 2's verdict. Blank means kept; anything else names why the item is
    #: out (`language`, `duplicate`, `spam`). Recorded rather than deleted, so a
    #: re-ingest of the same `external_id` cannot resurrect it as new, and so a
    #: heuristic that starts over-rejecting is visible instead of invisible.
    excluded_reason = models.CharField(max_length=32, blank=True)

    engagement_score = models.FloatField(default=0)
    velocity_score = models.FloatField(default=0)
    recency_score = models.FloatField(default=0)
    authority_score = models.FloatField(default=0)
    composite_score = models.FloatField(default=0)

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    cluster = models.ForeignKey(
        "trends.TrendCluster", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="items",
    )

    class Meta:
        ordering: ClassVar[list[str]] = ["-composite_score", "-posted_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="unique_trend_item_per_source"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["source", "-posted_at"]),
            models.Index(fields=["cluster", "-composite_score"]),
        ]

    def __str__(self) -> str:
        return f"{self.source_id}:{self.external_id}"

    @property
    def is_kept(self) -> bool:
        return not self.excluded_reason


class TrendCluster(models.Model):
    """A group of items saying the same thing, and how hard they are saying it.

    This is what grounds generation (design.md §8.3) and what `/app/trends`
    renders. `centroid` is the mean of its members' embeddings, kept so a later
    pass can attach a new item to an existing cluster without re-clustering the
    whole window.
    """

    category = models.ForeignKey(
        "categories.Category", on_delete=models.CASCADE, related_name="trend_clusters"
    )
    platform = models.CharField(max_length=32, choices=Platform.choices)
    label = models.CharField(max_length=200)
    centroid = VectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    item_count = models.PositiveIntegerField(default=0)
    velocity_score = models.FloatField(default=0)
    recency_score = models.FloatField(default=0)
    composite_score = models.FloatField(default=0)
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-composite_score"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["category", "platform", "-composite_score"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.platform})"

    @property
    def is_fresh(self) -> bool:
        """Resolved against the clock at read time, the same shape
        `Entitlements`'s trial lapse and `Reminder`'s token TTL already use —
        so freshness never depends on a periodic task having run."""
        return self.expires_at > timezone.now()

    @staticmethod
    def default_expiry(now: dt.datetime | None = None) -> dt.datetime:
        return (now or timezone.now()) + dt.timedelta(days=CACHE_TTL_DAYS)
