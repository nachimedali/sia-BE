"""Measurement (design.md §6.7, §8.9; implementation.md Phase 11).

Four tables closing the loop the rest of the system opened: what each published
copy earned (`PostMetric`), what the audience said (`AudienceComment`), how the
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

from typing import Any, ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from common.records import AppendOnly


class ImmutableCapture(AppendOnly):
    """A row recording what was true at one moment."""

    append_only_hint = "capture another row instead of editing this one."

    class Meta:
        abstract = True


class Availability(models.TextChoices):
    """Why a metric has no value — the distinction C-07 exists to preserve.

    `UNAVAILABLE` and a measured zero must never be stored the same way. A
    platform that reports nothing is not a platform that measured nothing, and
    every percentile, benchmark and finding downstream inherits the error if
    they are conflated.
    """

    MEASURED = "MEASURED", "Measured"
    UNAVAILABLE = "UNAVAILABLE", "Provider cannot report this"
    PENDING = "PENDING", "Provider sync still in flight"


class MetricSource(models.TextChoices):
    """Where a row's numbers came from.

    The reason this exists rather than being inferred from `provider_key`:
    Part 7 rule 17 has to be enforceable by a queryset. `analysable` filters
    on this, so no digest, finding, rule proposal or benchmark can be built
    from a fabricated row even by a caller who never considered the question.
    """

    PROVIDER = "PROVIDER", "A measurement provider"
    FAKE = "FAKE", "The test fake — never insight"


class PostMetricQuerySet(models.QuerySet["PostMetric"]):
    def measured(self) -> PostMetricQuerySet:
        """Rows carrying an actual reading.

        `UNAVAILABLE` and `PENDING` rows record that we asked and got no
        answer. They exist so a gap is visible, and they must never reach a
        denominator — a platform that reports nothing would otherwise drag
        every percentile it appears in toward zero.
        """
        return self.filter(availability=Availability.MEASURED)

    def real(self) -> PostMetricQuerySet:
        return self.filter(source=MetricSource.PROVIDER)

    def analysable(self) -> PostMetricQuerySet:
        """Measured *and* real. The only queryset statistics may read."""
        return self.measured().real()


class PostMetric(ImmutableCapture):
    post_target = models.ForeignKey(
        "content.PostTarget", on_delete=models.CASCADE, related_name="metrics"
    )
    captured_at = models.DateTimeField()

    #: **Null, never zero** (C-07, Part 7 rule 12). A platform that does not
    #: report a number leaves it `None`; a platform that reports zero stores
    #: `0`. Before P0-38 both were `0`, which is why every aggregate below
    #: reads through `analysable()` rather than trusting the column alone.
    impressions = models.PositiveIntegerField(null=True, blank=True)
    likes = models.PositiveIntegerField(null=True, blank=True)
    comments = models.PositiveIntegerField(null=True, blank=True)
    shares = models.PositiveIntegerField(null=True, blank=True)
    clicks = models.PositiveIntegerField(null=True, blank=True)
    saves = models.PositiveIntegerField(null=True, blank=True)
    #: Weighted interactions ÷ impressions, or ÷ followers where the platform
    #: does not report impressions (§8.9). Stored rather than derived so a
    #: later change to the weighting cannot silently rewrite history. `None`
    #: when neither denominator was available — the same rule as the counts.
    engagement_rate = models.FloatField(null=True, blank=True)

    #: Post-level reaction breakdown, e.g. `{"like": 40, "celebrate": 6}`
    #: (L-4a). `None` means the platform does not break reactions down; `{}`
    #: means it does and there were none. Depth is tiered by plan — total only,
    #: per-type, or per-type plus reactor list — but the *storage* is always
    #: whatever the provider gave, with the tier applied on read.
    reactions = models.JSONField(null=True, blank=True)

    #: Provenance (P0-29, A-16). Which provider answered, under which response
    #: mapping, and whether it answered at all.
    availability = models.CharField(
        max_length=12, choices=Availability.choices, default=Availability.MEASURED
    )
    source = models.CharField(
        max_length=8, choices=MetricSource.choices, default=MetricSource.PROVIDER
    )
    provider_key = models.CharField(max_length=32, blank=True)
    schema_version = models.PositiveSmallIntegerField(default=0)

    #: The provider's answer, verbatim and compressed (C-07b). `raw` is the
    #: pre-P0-39 uncompressed column, still dual-written; it contracts away
    #: once every live row carries `raw_payload`.
    raw = models.JSONField(default=dict, blank=True)
    raw_payload = models.BinaryField(default=bytes, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[models.Manager[PostMetric]] = PostMetricQuerySet.as_manager()

    @property
    def payload(self) -> Any:
        """The stored provider response, decompressed. Falls back to the
        legacy `raw` column so a reprocess spans the dual-write window."""
        from common.compression import unpack

        return unpack(self.raw_payload) if self.raw_payload else (self.raw or None)

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


class AudienceCommentQuerySet(models.QuerySet["AudienceComment"]):
    def measured(self) -> AudienceCommentQuerySet:
        """Threads we could actually read.

        An `UNAVAILABLE` marker row says "this platform does not expose
        comments" — TikTok, at any price (L-3). It is not a comment and must
        never sit in a denominator: a comment-rate that counted TikTok targets
        as having zero comments would be measuring our vendor's coverage and
        calling it audience behaviour (P0-03).
        """
        return self.filter(availability=Availability.MEASURED)


class AudienceComment(ImmutableCapture):
    """What the audience said, as they said it — the post-publish surface.

    **Never merged with the internal discussion thread** (C-03, L-3). Phase 2
    adds `Thread`/`Comment` for the team talking to itself before a post goes
    out; this is the public talking about it afterwards. Two models, two
    lists, one rule: a schema that lets them share a table will eventually
    leak one into the other, and the leak direction that matters is internal
    review notes appearing on a published post.

    Immutable for a second reason beyond the capture argument: a comment
    edited on our side would no longer be what the platform holds, and the
    sentiment aggregate would be describing text nobody wrote.

    The physical table is still `analytics_comment`. The model was renamed
    when it was promoted to first class (P0-02) and the table deliberately was
    not — moving it would buy nothing and would make the rename a data
    migration instead of a no-op.
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

    #: Per-type breakdown where the platform reports one, e.g.
    #: `{"like": 12, "celebrate": 3}`. Empty means "asked, none"; the
    #: unavailable case is carried by `availability`, not by an empty dict.
    reactions = models.JSONField(default=dict, blank=True)
    availability = models.CharField(
        max_length=12, choices=Availability.choices, default=Availability.MEASURED
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[models.Manager[AudienceComment]] = AudienceCommentQuerySet.as_manager()

    class Meta:
        db_table = "analytics_comment"
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
    #: Null, never zero — the same rule as `PostMetric` (C-07). An account the
    #: provider could not read is not an account with no followers, and this
    #: number is a *denominator* downstream, which makes a fabricated zero
    #: worse here than almost anywhere else.
    followers = models.PositiveIntegerField(null=True, blank=True)
    following = models.PositiveIntegerField(null=True, blank=True)
    total_posts = models.PositiveIntegerField(null=True, blank=True)

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


class ProviderCursor(models.Model):
    """Where the delta feed was last read from, per provider (P0-31).

    One row per provider, not per account: `/v1/analytics/delta` is a single
    feed across every connected account, which is the whole reason it replaces
    per-post polling.

    `bootstrapped_at` is load-bearing. The feed is a rolling seven-day log and
    cannot replay history, so following it without having first bootstrapped
    from `/v1/analytics` silently misses every post older than the window. A
    null here means "not bootstrapped", and the follower refuses to advance
    until the ladder has run.
    """

    provider_key = models.CharField(max_length=32, unique=True)
    cursor = models.CharField(max_length=512, blank=True)
    bootstrapped_at = models.DateTimeField(null=True, blank=True)
    #: Consecutive reads that returned nothing. Not an error — a quiet feed is
    #: normal — but a feed that has been silent for days while posts are
    #: publishing is a broken integration, and this is what makes that visible.
    empty_reads = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.provider_key} @ {self.cursor or '(start)'}"


class AudienceCaptureState(models.Model):
    """When this target's audience comments were last read (P0-34, L-4a).

    Stored rather than derived from the newest `AudienceComment`: a post with
    no new comments would otherwise look like a post that was never polled,
    and the plan's cadence would be ignored precisely on the quiet posts where
    polling is pure waste.
    """

    post_target = models.OneToOneField(
        "content.PostTarget", on_delete=models.CASCADE, related_name="audience_state"
    )
    last_captured_at = models.DateTimeField(null=True, blank=True)
    #: Set when the platform has told us it will never report comments here,
    #: so the cadence check does not keep waking a target that cannot answer.
    unavailable = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.post_target_id} @ {self.last_captured_at or 'never'}"


class CaptureDeferral(models.Model):
    """How many times one rung has been put off for want of provider budget
    (P0-44, C-06).

    A deferral is not a failure — it costs nothing and the next tick retries —
    but a rung deferred over and over is a *gap*, and a gap that nobody records
    silently shrinks the sample size behind a confidence grade. At
    `MAX_DEFERRALS` the capture is abandoned and an `UNAVAILABLE` snapshot is
    written in its place, which is what takes it out of every denominator
    through the queryset rather than through anyone's memory.

    Rows are cleared when the rung is finally captured, so a healthy target
    accumulates nothing.
    """

    #: Three consecutive windows, per C-06. Beyond this the number would be
    #: stale enough that recording the gap is more honest than recording the
    #: reading.
    MAX_DEFERRALS = 3

    post_target = models.ForeignKey(
        "content.PostTarget", on_delete=models.CASCADE, related_name="capture_deferrals"
    )
    rung = models.DateTimeField()
    count = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["post_target", "rung"], name="unique_deferral_per_rung")
        ]

    def __str__(self) -> str:
        return f"{self.post_target_id} @ {self.rung:%Y-%m-%d %H:%M} x{self.count}"


class MetricCapability(models.Model):
    """What a given provider can actually report, per platform, per metric.

    Small table, large consequence. It is keyed by provider as well as platform
    because coverage is a property of the pair: the same platform reports
    different things through different vendors, and a swap that silently
    inherited the old vendor's capability map would fabricate coverage.

    Seeded and admin-editable like every other operational table, so a vendor
    adding an endpoint is a row edit rather than a deploy.
    """

    provider_key = models.CharField(max_length=32)
    platform = models.CharField(max_length=32)
    metric_key = models.CharField(max_length=64)
    available = models.BooleanField(default=True)
    #: Documented lag before the number settles — Instagram demographics run up
    #: to 48h behind. A capture inside the hint is `PENDING`, not `UNAVAILABLE`:
    #: one reschedules, the other is recorded as a permanent gap.
    latency_hint = models.DurationField(null=True, blank=True)
    note = models.CharField(max_length=280, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["provider_key", "platform", "metric_key"],
                name="unique_provider_platform_metric",
            )
        ]
        ordering: ClassVar[list[str]] = ["provider_key", "platform", "metric_key"]
        verbose_name_plural = "metric capabilities"

    def __str__(self) -> str:
        state = "available" if self.available else "unavailable"
        return f"{self.provider_key}/{self.platform}/{self.metric_key} ({state})"


class DemographicDimension(models.TextChoices):
    """What a breakdown is *of*. A closed set, because a dimension the surface
    cannot label is a chart with no axis."""

    AGE = "AGE", "Age"
    GENDER = "GENDER", "Gender"
    COUNTRY = "COUNTRY", "Country"
    CITY = "CITY", "City"
    LANGUAGE = "LANGUAGE", "Language"


class AudienceDemographic(ImmutableCapture):
    """Who follows an account, captured on the **existing** daily snapshot
    (P6-01) — not a new ladder.

    Hung off the 01:30 job deliberately: demographics move slowly and the
    provider lags up to 48 hours, so a tighter cadence would spend quota to
    re-read numbers that have not changed.

    **Availability is not optional here.** The provider needs ≥100 followers
    before it will report a breakdown at all (L-5), and an account below that
    threshold must render as *unavailable* rather than as a chart of zeros —
    "we cannot see your audience yet" and "your audience is nobody" are
    different statements, and only one of them is true.
    """

    social_account = models.ForeignKey(
        "channels.SocialAccount", on_delete=models.CASCADE, related_name="demographics"
    )
    captured_at = models.DateTimeField()
    dimension = models.CharField(max_length=16, choices=DemographicDimension.choices)
    #: `{bucket: share}` — e.g. `{"25-34": 0.41}`. Null on an `UNAVAILABLE`
    #: row, which carries no numbers at all rather than zeros.
    breakdown = models.JSONField(null=True, blank=True)
    availability = models.CharField(
        max_length=12, choices=Availability.choices, default=Availability.MEASURED
    )
    provider_key = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-captured_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["social_account", "dimension", "captured_at"],
                name="one_demographic_per_account_dimension_capture",
            )
        ]

    def __str__(self) -> str:
        return f"{self.social_account_id} {self.dimension} ({self.availability})"


# -----------------------------------------------------------------------------
# Reporting (Phase 6)
# -----------------------------------------------------------------------------
class ReportSchedule(models.TextChoices):
    NONE = "NONE", "On demand only"
    MONTHLY = "MONTHLY", "Every month"


class Report(models.Model):
    """A saved, **declarative** report (P6-04).

    `sections` is a list of `{kind, options}` — what to include, never how to
    draw it. Rendering lives in `services.reporting` and the render port, so
    the same definition produces the on-screen view and the PDF. A report whose
    stored shape was a layout would have to be migrated every time the design
    moved.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="reports"
    )
    name = models.CharField(max_length=120)
    sections = models.JSONField(default=list, blank=True)
    schedule = models.CharField(
        max_length=8, choices=ReportSchedule.choices, default=ReportSchedule.NONE
    )
    #: How far back a run covers. Bounded by `analytics_history_days` at render
    #: time rather than here, because a plan change must not silently rewrite a
    #: saved report — it should fail loudly the next time it runs (P6-09).
    window_days = models.PositiveIntegerField(default=30)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "name"], name="report_name_is_unique_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return self.name


class ReportRunStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


class ReportRun(models.Model):
    """One rendering of one report over one window.

    Kept rather than regenerated on demand: a number a client was shown last
    month must still say what it said, even after later captures change the
    totals. A report that silently re-renders is a report nobody can cite.
    """

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="runs")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(
        max_length=8, choices=ReportRunStatus.choices, default=ReportRunStatus.PENDING
    )
    #: The rendered sections, exactly as they were computed. The PDF is derived
    #: from this, so what a viewer reads on screen and what they download are
    #: the same numbers by construction rather than by two code paths agreeing.
    payload = models.JSONField(default=dict, blank=True)
    document = models.FileField(upload_to="reports/", blank=True)
    error_detail = models.JSONField(default=dict, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="report_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["report", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.report_id} {self.window_start:%Y-%m-%d} ({self.status})"


class ReportShareLink(models.Model):
    """A report sent to somebody with no account (P6-06).

    **The Phase 2 `GUEST_VIEW` pattern, reused rather than reinvented**: minted
    at send, only the SHA-256 digest stored, expiring, revocable, and resolved
    by one lookup that is itself the access control. The digest comes from
    `common.tokens`, so a review link and a report share cannot end up hashing
    differently.

    Multi-use like `GUEST_VIEW`, and revocable for the same reason: a link that
    cannot expire by being consumed has no other way to be closed.
    """

    run = models.ForeignKey(ReportRun, on_delete=models.CASCADE, related_name="share_links")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    email = models.EmailField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="report_share_links",
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]

    def __str__(self) -> str:
        return f"share of run {self.run_id}"

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()
