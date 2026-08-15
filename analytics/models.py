"""Measurement (design.md §6.7, §8.9; implementation.md Phase 11).

Four tables closing the loop the rest of the system opened: what each published
copy earned (`PostMetric`), what people said about it (`Comment`), how the
account itself moved (`AccountSnapshot`), and which old post is worth running
again (`RepurposeCandidate`).

**Metric and comment rows are immutable.** A capture is a statement about a
moment — "at T+6h this post had 412 likes" — and editing it would destroy the
only thing the engagement-decay slope in §8.9 can be computed from. The guard is
`common.records.AppendOnly`, shared with the ledgers (I4) — same rule, same
exception, different columns. It stops at a model-level guard rather than a DB
trigger because, unlike the ledgers, nothing here is money: a wrong metric is a
wrong chart, not a wrong invoice.

`PostMetric` hangs off `PostTarget`, not `Post`: the same master post published
to Instagram and LinkedIn earns differently on each, and the whole point of
format attribution is being able to see that.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models
from django.utils import timezone

from common.records import AppendOnly


class ImmutableCapture(AppendOnly):
    """A row recording what was true at one moment."""

    append_only_hint = "capture another row instead of editing this one."

    class Meta:
        abstract = True


class PostMetric(ImmutableCapture):
    post_target = models.ForeignKey(
        "content.PostTarget", on_delete=models.CASCADE, related_name="metrics"
    )
    captured_at = models.DateTimeField()
    impressions = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)
    comments = models.PositiveIntegerField(default=0)
    shares = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    saves = models.PositiveIntegerField(default=0)
    #: Weighted interactions ÷ impressions, or ÷ followers where the platform
    #: does not report impressions (§8.9). Stored rather than derived so a
    #: later change to the weighting cannot silently rewrite history.
    engagement_rate = models.FloatField(default=0)
    raw = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-captured_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["post_target", "captured_at"], name="unique_metric_per_capture"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["post_target", "-captured_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.post_target_id} @ {self.captured_at:%Y-%m-%d %H:%M}"


class Sentiment(models.TextChoices):
    POSITIVE = "POS", "Positive"
    NEUTRAL = "NEU", "Neutral"
    NEGATIVE = "NEG", "Negative"


class Comment(ImmutableCapture):
    """What someone said, as they said it.

    Immutable for a second reason beyond the capture argument: a comment edited
    on our side would no longer be what the platform holds, and the sentiment
    aggregate would be describing text nobody wrote.
    """

    post_target = models.ForeignKey(
        "content.PostTarget", on_delete=models.CASCADE, related_name="post_comments"
    )
    external_id = models.CharField(max_length=200)
    author = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    sentiment = models.CharField(max_length=3, choices=Sentiment.choices, default=Sentiment.NEUTRAL)
    sentiment_score = models.FloatField(default=0)
    posted_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-posted_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["post_target", "external_id"], name="unique_comment_per_target"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["post_target", "sentiment"]),
        ]

    def __str__(self) -> str:
        return f"{self.author}: {self.body[:40]}"


class AccountSnapshot(ImmutableCapture):
    social_account = models.ForeignKey(
        "channels.SocialAccount", on_delete=models.CASCADE, related_name="snapshots"
    )
    captured_at = models.DateTimeField()
    followers = models.PositiveIntegerField(default=0)
    following = models.PositiveIntegerField(default=0)
    total_posts = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-captured_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["social_account", "captured_at"], name="unique_snapshot_per_capture"
            )
        ]

    def __str__(self) -> str:
        return f"{self.social_account_id} @ {self.captured_at:%Y-%m-%d}"


class RepurposeReason(models.TextChoices):
    EVERGREEN = "EVERGREEN", "Kept earning"
    SPIKE_STEADY = "SPIKE_STEADY", "Spiked, then held"


class RepurposeCandidate(models.Model):
    """An old post worth running again (§8.9).

    Not immutable, unlike its neighbours here: this one is a *suggestion*, and
    `dismissed_at`/`reissued_post` are the user answering it. The nightly scan
    refreshes an open candidate's score in place rather than stacking a second
    row for the same post, so the queue stays one row per post.
    """

    post = models.ForeignKey(
        "content.Post", on_delete=models.CASCADE, related_name="repurpose_candidates"
    )
    percentile = models.FloatField(default=0)
    reason = models.CharField(
        max_length=16, choices=RepurposeReason.choices, default=RepurposeReason.EVERGREEN
    )
    score = models.FloatField(default=0)
    #: When the post this came from actually went out — the date the 60-day age
    #: rule was applied to. Stored because `Post` has no publish time of its own
    #: (only `PostTarget` does) and `Post.updated_at` is a row mtime that moves
    #: every time the post is touched, including by accepting this suggestion.
    published_at = models.DateTimeField(null=True, blank=True)
    surfaced_at = models.DateTimeField(default=timezone.now)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    #: Set when the user accepts and a `REPURPOSE` generation produces a new
    #: post. Also what "no reissue in 90 days" is measured against.
    reissued_post = models.ForeignKey(
        "content.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="repurposed_from",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-score", "-surfaced_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One open candidate per post. A post already accepted or dismissed
            # can be surfaced again later — that is the 90-day rule's job to
            # decide, not the schema's — so the constraint is partial.
            models.UniqueConstraint(
                fields=["post"],
                condition=models.Q(dismissed_at__isnull=True, reissued_post__isnull=True),
                name="unique_open_repurpose_candidate_per_post",
            )
        ]

    def __str__(self) -> str:
        return f"repurpose {self.post_id} ({self.score:.2f})"


class RepurposeConfig(models.Model):
    """How eager the repurpose queue is (§8.9: "admin-tunable").

    A singleton, the same shape and the same reasoning as
    `ai.models.QualityGateConfig`: a deploy is the wrong unit of change for a
    tuning dial. Not an I8 quota — I8 governs what a customer's money buys, and
    no plan may change this.

    It lives here rather than beside the service that reads it because Django
    imports `<app>.models` and nothing else to populate the app registry: a
    model declared in a service module only registers as a side effect of
    something importing that module, which works until the day it does not.
    """

    percentile_threshold = models.FloatField(default=80.0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "repurpose configuration"
        verbose_name_plural: ClassVar[str] = "repurpose configuration"

    def __str__(self) -> str:
        return "Repurpose configuration"

    @classmethod
    def get_solo(cls) -> RepurposeConfig:
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance
