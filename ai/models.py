"""Generation (design.md §6.5, §8.3, I1, I2, I7).

`Generation.trend_cluster`/`recipe` — design.md §6.5 lists `trend_cluster→
TrendCluster (null)` and `recipe→CreativeRecipe (null)`; both models arrive in
Phase 10/14. Deferred the same way `Post.product`/`generation` were before
this phase (A32/A47/A48/A60).

`GenerationMode` declares the full enum design.md names, but only a subset is
actually invocable in Phase 7 — `ai.services.pipeline.ALLOWED_MODES` is what
enforces that (design.md §15.8 A69), the same "declare the full enum, gate
what's reachable" shape `PostStatus` used in Phase 4 (A47).
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models


class GenerationKind(models.TextChoices):
    TEXT = "TEXT", "Text"
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class GenerationMode(models.TextChoices):
    IDEA = "IDEA", "Idea"
    TREND = "TREND", "Trend"
    REPURPOSE = "REPURPOSE", "Repurpose"
    REWRITE = "REWRITE", "Rewrite"
    PRODUCT = "PRODUCT", "Product"
    AUTOPILOT = "AUTOPILOT", "Autopilot"
    RECIPE = "RECIPE", "Recipe"
    REVISION = "REVISION", "Revision"


class GenerationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class VoiceProfile(models.Model):
    """design.md §6.5. `exemplar_post_ids` is a plain id list rather than an
    m2m — exemplars are a handful of the workspace's own posts picked for
    prompt grounding, not a relationship anything queries from the other
    side."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="voice_profiles"
    )
    name = models.CharField(max_length=120)
    tone_descriptors = models.JSONField(default=list, blank=True)
    banned_phrases = models.JSONField(default=list, blank=True)
    exemplar_post_ids = models.JSONField(default=list, blank=True)
    system_prompt = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["name"]

    def __str__(self) -> str:
        return self.name


class Generation(models.Model):
    """One request to a provider, grounded and quality-gated (design.md §8.3).

    `credits_charged`/`video_units_charged` record what was actually debited
    — the ledger row is the source of truth (I4), these are a denormalised
    read for "what did this generation cost" without joining the ledger.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="generations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="generations"
    )
    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    mode = models.CharField(max_length=16, choices=GenerationMode.choices)
    prompt = models.TextField(blank=True)

    product = models.ForeignKey(
        "products.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generations",
    )
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generations",
    )
    voice_profile = models.ForeignKey(
        VoiceProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="generations"
    )
    parent_generation = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions"
    )

    output_type = models.CharField(max_length=32, blank=True)
    aspect = models.CharField(max_length=16, blank=True, default="1:1")
    render_style = models.CharField(max_length=64, blank=True)
    scene = models.CharField(max_length=200, blank=True)
    motion = models.CharField(max_length=64, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)

    is_batch = models.BooleanField(default=False)
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    credits_charged = models.PositiveIntegerField(default=0)
    video_units_charged = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=GenerationStatus.choices, default=GenerationStatus.PENDING
    )
    # Populated on the terminal FAILED state — the quality gate's rejection
    # reasons, or a provider error, surfaced to the caller without a join.
    error_detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.mode} {self.pk} ({self.status})"


class GenerationVariant(models.Model):
    """One candidate output. `was_selected` marks the one the user actually
    used (composed into a post, kept as the revision's basis) — a generation
    can produce several variants and none, one, or several may end up used."""

    generation = models.ForeignKey(Generation, on_delete=models.CASCADE, related_name="variants")
    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    body = models.TextField(blank=True)
    media_asset = models.ForeignKey(
        "content.MediaAsset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generation_variants",
    )
    platform = models.CharField(max_length=16, blank=True)
    rank = models.PositiveSmallIntegerField(default=0)
    rationale = models.CharField(max_length=300, blank=True)
    was_selected = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["rank", "id"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["generation", "rank"])]

    def __str__(self) -> str:
        return f"variant {self.pk} of generation {self.generation_id} (rank {self.rank})"


class GenerationCost(models.Model):
    """The §4.2 credit table, as rows (I8) — never a code constant.

    Resolution is exact `(kind, mode, provider, model)` → `(kind, mode)` →
    `(kind)`; no match is a hard error, never a silent zero (design.md A10).
    Blank `mode`/`provider`/`model` is the wildcard for a fallback row —
    `ai.services.costing.resolve_cost` is the only reader of this table.
    """

    kind = models.CharField(max_length=8, choices=GenerationKind.choices)
    mode = models.CharField(max_length=16, choices=GenerationMode.choices, blank=True)
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)
    credits = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["kind", "mode", "provider", "model"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["kind", "mode", "provider", "model"], name="unique_generation_cost_row"
            )
        ]

    def __str__(self) -> str:
        specificity = "/".join(filter(None, [self.mode, self.provider, self.model])) or "any"
        return f"{self.kind} ({specificity}): {self.credits} credits"


class QualityCheck(models.Model):
    """One quality-gate attempt (design.md §8.3). Every attempt is persisted,
    passed or not — the evaluation harness and I2's audit trail both read this
    table, and a rejected attempt is never shown to the user but is never
    silently discarded either."""

    generation = models.ForeignKey(
        Generation, on_delete=models.CASCADE, related_name="quality_checks"
    )
    variant = models.ForeignKey(
        GenerationVariant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quality_checks",
    )
    checks = models.JSONField(default=dict, blank=True)
    identity_score = models.FloatField(null=True, blank=True)
    passed = models.BooleanField()
    attempt = models.PositiveSmallIntegerField()
    rejected_reason = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["generation", "attempt"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["generation", "attempt"])]

    def __str__(self) -> str:
        outcome = "passed" if self.passed else "rejected"
        return f"generation {self.generation_id} attempt {self.attempt}: {outcome}"


class QualityGateConfig(models.Model):
    """Singleton (design.md §8.3: "admin-tunable threshold"). Not a `Plan`/
    `GenerationCost`-style per-transaction row (I8 governs commercial numbers;
    this is an engineering quality bar) — one row, edited in admin, read
    through `get_solo()` the same shape Phase 6's `PUBLISH_RESERVE_RATIO`
    reasoned about for its own constant (design.md A64), except this one *is*
    product-facing enough to warrant admin editing without a deploy."""

    identity_similarity_threshold = models.FloatField(default=0.6)
    max_regeneration_attempts = models.PositiveSmallIntegerField(default=3)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return "Quality gate configuration"

    @classmethod
    def get_solo(cls) -> QualityGateConfig:
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance
