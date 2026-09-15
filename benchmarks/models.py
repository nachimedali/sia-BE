"""Cohort benchmarks (BUILD-PLAN Phase 8).

A workspace learns how its posts compare with brands like it — same vertical,
market, platform and account size — without any brand's content, media or
identity leaving its tenant. Four kinds of row make that possible, and each
exists to hold one line the phase may not cross:

* `ConsentPolicy` / `ConsentRecord` — **opt-in, against terms somebody read.**
  Append-only on both sides: a policy is superseded by a new version, never
  edited under the agreements that cite it; a revocation is a new record, never
  an update to the grant.
* `BenchmarkObservation` — **the projection.** The only thing the aggregator
  reads, and deliberately unable to carry text, media or a tenant id.
  `tests/test_phase8_gates.py` fails if a column is added without review.
* `BenchmarkRun` / `CohortBenchmark` — **what was published, kept as it was.**
  Revoking consent stops future contribution and does not recompute these
  (P8-07); a number somebody was shown last month still says what it said.
* `BenchmarkConfig` — the thresholds, admin-editable, with floors the database
  enforces so no edit can make a cohort small enough to reverse.
"""

from __future__ import annotations

import itertools
from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from common.records import AppendOnly


class ConsentPolicy(AppendOnly):
    """The terms a workspace agrees to. **Written by legal, not by this code.**

    Nothing seeds a row. Until one is published through admin there is nothing
    to consent to and the grant endpoint refuses — which is Phase 8's entry
    gate (P8-01) held by the system itself rather than by a checklist.

    The highest `version` is the current policy. Publishing a new version makes
    every earlier grant stale until renewed: agreement to version 1 is not
    agreement to version 2, and carrying it forward would mean contributing
    under terms nobody at the workspace has read.
    """

    append_only_hint = "publish a new version instead of editing this one."

    version = models.PositiveIntegerField(unique=True)
    #: Plain language, shown beside the opt-in. The full terms live at
    #: `document_url`; this is what a person actually reads before clicking.
    summary = models.TextField()
    document_url = models.URLField(blank=True)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "consent policies"
        ordering: ClassVar[list[str]] = ["-version"]

    def __str__(self) -> str:
        return f"Benchmark terms v{self.version}"


class ConsentAction(models.TextChoices):
    GRANTED = "GRANTED", "Granted"
    REVOKED = "REVOKED", "Revoked"


class ConsentRecord(AppendOnly):
    """One grant or one revocation, timestamped (P8-05, A-13).

    **Events rather than a row with `granted_at` and `revoked_at`.** The plan
    sketches the latter, but writing `revoked_at` later is an update to an
    append-only row, and the history it would overwrite — granted, revoked,
    granted again under new terms — is exactly what a data-ownership question
    asks for. The current state is the newest record.
    """

    append_only_hint = "record a new grant or revocation instead of editing this one."

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="benchmark_consents"
    )
    policy = models.ForeignKey(ConsentPolicy, on_delete=models.PROTECT, related_name="records")
    action = models.CharField(max_length=8, choices=ConsentAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="benchmark_consents",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-recorded_at", "-id"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-recorded_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} v{self.policy_id} by workspace {self.workspace_id}"


def _validate_edges(value: Any) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(edge, int) or isinstance(edge, bool) or edge <= 0 for edge in value)
        or any(later <= earlier for earlier, later in itertools.pairwise(value))
    ):
        raise ValidationError(
            "Size band edges must be a non-empty, increasing list of positive integers."
        )


def _default_edges() -> list[int]:
    return [1_000, 10_000, 100_000, 1_000_000]


class BenchmarkConfig(models.Model):
    """The thresholds (P8-04). A singleton, like `RepurposeConfig`.

    Admin-editable because they are policy, and a deploy is the wrong unit of
    change for policy. The database still refuses two edits no policy may make:
    a cohort of fewer than three workspaces, in which each member can subtract
    itself from the aggregate and read the others; and fewer posts than
    workspaces, which is not a threshold but a contradiction.
    """

    min_workspaces = models.PositiveIntegerField(default=8)
    min_posts = models.PositiveIntegerField(default=200)
    #: One brand's share of a cohort is capped, most recent posts first. Without
    #: it, eight brands where one posts four hundred times produce that one
    #: brand's median under a cohort's name.
    max_posts_per_contributor = models.PositiveIntegerField(default=50)
    window_days = models.PositiveIntegerField(default=90)
    #: Posts younger than this are still accruing engagement, and counting them
    #: pulls every median toward the posts nobody has seen yet.
    min_post_age_days = models.PositiveIntegerField(default=7)
    #: Follower counts where one band ends and the next begins, lower-inclusive.
    #: `[1000, 10000]` yields `0_1000`, `1000_10000`, `10000_plus`.
    size_band_edges = models.JSONField(default=_default_edges, validators=[_validate_edges])

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "benchmark configuration"
        verbose_name_plural: ClassVar[str] = "benchmark configuration"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(min_workspaces__gte=3), name="benchmark_min_workspaces_floor"
            ),
            models.CheckConstraint(
                condition=models.Q(min_posts__gte=models.F("min_workspaces")),
                name="benchmark_min_posts_covers_workspaces",
            ),
            models.CheckConstraint(
                condition=models.Q(max_posts_per_contributor__gte=1),
                name="benchmark_contributor_cap_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(window_days__gt=models.F("min_post_age_days")),
                name="benchmark_window_outlasts_post_age",
            ),
        ]

    def __str__(self) -> str:
        return "Benchmark configuration"

    @classmethod
    def get_solo(cls) -> BenchmarkConfig:
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance


class BenchmarkObservation(models.Model):
    """One contributed post, reduced to what a cohort statistic reads (P8-06).

    **The structural half of the privacy rule.** There is no column here that
    could hold a caption, a media reference, a post, account, workspace or user
    id, or an edit diff. A contributor is a keyed hash, stable enough to count
    distinct brands and useless for naming one without the application's secret.
    The aggregator imports this model and nothing from any tenant app, so the
    question "could it read raw content?" is answered by what exists, not by
    what a query happens to select.

    Not append-only: this is a derived table, rebuilt per contributor on every
    projection run, and deleted for a contributor the moment they revoke.
    """

    contributor = models.CharField(max_length=64)
    vertical = models.ForeignKey("categories.Category", on_delete=models.CASCADE, related_name="+")
    market = models.CharField(max_length=2)
    platform = models.CharField(max_length=16)
    size_band = models.CharField(max_length=32)
    post_format = models.CharField(max_length=16)
    posting_window = models.CharField(max_length=16)
    published_on = models.DateField()

    #: Null, never zero (Part 7 rule 12). A platform that reports engagement
    #: but not impressions contributes its engagement rate and nothing else.
    engagement_rate = models.FloatField(null=True, blank=True)
    reach_rate = models.FloatField(null=True, blank=True)
    comment_rate = models.FloatField(null=True, blank=True)

    projected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["vertical", "market", "platform", "size_band", "post_format"],
                name="benchmark_obs_cohort_idx",
            ),
            models.Index(fields=["contributor"], name="benchmark_obs_contributor_idx"),
            models.Index(fields=["published_on"], name="benchmark_obs_published_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}/{self.post_format} {self.published_on}"


class BenchmarkRun(AppendOnly):
    """One nightly computation, and the thresholds it was computed under.

    The thresholds are copied onto the run because they are admin-editable: a
    shortfall shown against today's threshold for a cohort computed under last
    month's would be a number that was never true.
    """

    append_only_hint = "run the aggregation again to produce a new run."

    computed_at = models.DateTimeField(auto_now_add=True)
    window_start = models.DateField()
    window_end = models.DateField()
    min_workspaces = models.PositiveIntegerField()
    min_posts = models.PositiveIntegerField()
    max_posts_per_contributor = models.PositiveIntegerField()

    class Meta:
        ordering: ClassVar[list[str]] = ["-computed_at", "-id"]

    def __str__(self) -> str:
        return f"Benchmark run {self.pk} ({self.window_start} to {self.window_end})"


class CohortBenchmark(AppendOnly):
    """One cohort's distribution in one run (P8-02, P8-03).

    **`sufficient` false means no statistic, enforced by the database** (P8-G1).
    Counts are kept either way so the read can state the shortfall; `metrics`
    and `best_windows` are empty unless the cohort cleared both thresholds. A
    constraint rather than a convention, because the failure it prevents — a
    median computed from three brands — is invisible in any single response.
    """

    append_only_hint = "run the aggregation again to produce a new run."

    run = models.ForeignKey(BenchmarkRun, on_delete=models.CASCADE, related_name="cohorts")
    vertical = models.ForeignKey("categories.Category", on_delete=models.CASCADE, related_name="+")
    market = models.CharField(max_length=2)
    platform = models.CharField(max_length=16)
    size_band = models.CharField(max_length=32)
    post_format = models.CharField(max_length=16)

    contributors = models.PositiveIntegerField()
    posts = models.PositiveIntegerField()
    sufficient = models.BooleanField()

    #: `{metric: {median, p25, p75, posts}}`, only for metrics that cleared the
    #: thresholds on their own non-null values.
    metrics = models.JSONField(default=dict, blank=True)
    #: `{metric: {contributors, posts}}` for every metric, so a withheld metric
    #: can say how far short it is.
    metric_counts = models.JSONField(default=dict, blank=True)
    #: `[{window, median_engagement_rate, posts}]`, best first, each window
    #: itself drawn from at least `min_workspaces` contributors.
    best_windows = models.JSONField(default=list, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["platform", "post_format", "size_band"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run", "vertical", "market", "platform", "size_band", "post_format"],
                name="unique_cohort_per_run",
            ),
            models.CheckConstraint(
                condition=models.Q(sufficient=True)
                | (models.Q(metrics={}) & models.Q(best_windows=[])),
                name="insufficient_cohort_carries_no_statistic",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.platform}/{self.post_format}/{self.size_band} in run {self.run_id}"
