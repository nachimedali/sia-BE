"""The Playbook's corpus: public posts that are doing well right now.

The Playbook is a wall of trending public posts a brand can pick from and
compose its own with (`/app/playbook`). Two free providers fill it, ported from
the trendgen collectors in `feed/` with the same rules (`playbook.collectors`):
YouTube's Data API and Google Trends' public RSS.

**Not a second trend pipeline.** `trends/` clusters a category's corpus into
themes that ground every generation; this corpus is never clustered, never
grounds a prompt on its own and never feeds a benchmark. It is a library of
individual references a person chooses from. When trendgen serves `GET
/v1/feeds` (Phase 12) this app's collectors are what move there, which is why
they keep `feed/`'s port shape rather than `trends/`'s.

**A pointer, not a copy.** A post row keeps the link, the public numbers and
the structure derived from them. It never re-hosts the creator's media: the
thumbnail is a URL on the provider's own image host, read at composition time
and never stored (`ai.services.references`).

**A count the provider did not report is null, never 0** (rule 4). YouTube
publishes views, likes and comments and nothing else; Google Trends publishes
a traffic bucket and no engagement at all.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models


class Provider(models.TextChoices):
    YOUTUBE = "youtube", "YouTube"
    GOOGLE_TRENDS = "google_trends", "Google Trends"


class PostFormat(models.TextChoices):
    #: A video under a minute — YouTube's own Shorts ceiling was a minute when
    #: the format launched, and "under a minute" is a fact about the clip
    #: rather than a guess about how YouTube classified it.
    SHORT = "SHORT", "Short video"
    VIDEO = "VIDEO", "Video"
    #: A search that is rising today. It has a topic and news coverage, not a
    #: picture of its own, so it is referenced for its topic only.
    SEARCH = "SEARCH", "Search trend"


class PlaybookSource(models.Model):
    """Where to look: one provider, one market, optionally one category.

    `config` holds limits and queries — operator-tunable data — and never a
    credential (`providers.md`: keys live in the environment).
    """

    provider = models.CharField(max_length=24, choices=Provider.choices)
    #: ISO 3166-1 alpha-2, the provider's own region code.
    region = models.CharField(max_length=2)
    #: A root category for a source that asks a category's question (a YouTube
    #: search). Null for a source that answers for the whole market (YouTube's
    #: most popular chart, Google Trends' daily list).
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="playbook_sources",
    )
    label = models.CharField(max_length=120, blank=True)
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    #: How often this source is worth asking. A YouTube search costs 100 quota
    #: units, the chart 1; the daily quota is 10,000 — so the cadence is a row
    #: an operator retunes, never a constant.
    refresh_minutes = models.PositiveIntegerField(default=240)

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["region", "provider", "category__name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Postgres treats nulls as distinct, so the market-wide source
            # needs its own constraint or it could be seeded twice.
            models.UniqueConstraint(
                fields=["provider", "region", "category"],
                condition=models.Q(category__isnull=False),
                name="unique_playbook_category_source",
            ),
            models.UniqueConstraint(
                fields=["provider", "region"],
                condition=models.Q(category__isnull=True),
                name="unique_playbook_market_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}/{self.region}/{self.category_id or '*'}"


class PlaybookPost(models.Model):
    """One public post, as its provider described it at the last read.

    `source` is the last source that saw it; `category` is kept from a
    category's source when one found it, because the market-wide chart cannot
    say what a video is about."""

    source = models.ForeignKey(PlaybookSource, on_delete=models.CASCADE, related_name="posts")
    provider = models.CharField(max_length=24, choices=Provider.choices)
    region = models.CharField(max_length=2)
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="playbook_posts",
    )
    external_id = models.CharField(max_length=200)
    format = models.CharField(max_length=8, choices=PostFormat.choices)

    url = models.URLField(max_length=500)
    #: On the provider's own image host. Shown by link and read at composition
    #: time; never downloaded into storage.
    thumbnail_url = models.URLField(max_length=1000, blank=True)
    #: Who the picture belongs to when it is not the creator's — Google Trends
    #: illustrates a search with a news outlet's photograph.
    thumbnail_credit = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=300)
    caption = models.TextField(blank=True)
    creator = models.CharField(max_length=200, blank=True)
    hashtags = models.JSONField(default=list, blank=True)
    lang = models.CharField(max_length=12, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    posted_at = models.DateTimeField()

    views = models.BigIntegerField(null=True, blank=True)
    likes = models.BigIntegerField(null=True, blank=True)
    comments = models.BigIntegerField(null=True, blank=True)
    #: Google Trends' "200,000+": the lower bound of a bucket, never a count.
    traffic_floor = models.BigIntegerField(null=True, blank=True)
    traffic_label = models.CharField(max_length=40, blank=True)
    #: News stories Google attaches to a search: `[{title, url, source}]`.
    news = models.JSONField(default=list, blank=True)

    #: Derived at the last scoring pass (`playbook.services.scoring`). All
    #: recomputable from the columns above; null where the inputs are.
    velocity = models.FloatField(null=True, blank=True)
    engagement = models.FloatField(null=True, blank=True)
    trend_score = models.PositiveSmallIntegerField(null=True, blank=True)
    structure = models.JSONField(default=list, blank=True)

    #: The provider's response for this item, kept so scoring can be replayed.
    payload = models.JSONField(default=dict, blank=True)
    #: Made by the fake collector. A test fixture: the API never serves one
    #: unless `PLAYBOOK_SHOW_SAMPLE` is on, which production settings refuse.
    is_fake = models.BooleanField(default=False)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()

    class Meta:
        ordering: ClassVar[list[str]] = ["-trend_score", "-posted_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One row per post per market, whichever source saw it: the chart
            # and a category search often find the same video, and the wall
            # must not show it twice.
            models.UniqueConstraint(
                fields=["region", "provider", "external_id"], name="unique_playbook_post"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["region", "-posted_at"]),
            models.Index(fields=["region", "provider", "-trend_score"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.external_id}"

    @property
    def can_guide_pictures(self) -> bool:
        """Whether its picture may be shown to the image model.

        A creator's own thumbnail, yes — as a composition reference. A news
        outlet's photograph illustrating a search, no: it is not the trend, and
        it is not the creator's to lend. Such a post contributes its topic.
        """
        return self.format != PostFormat.SEARCH and bool(self.thumbnail_url)
