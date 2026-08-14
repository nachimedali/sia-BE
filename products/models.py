"""Products and autopilot (design.md §6.4, D7, §8.7).

The `Product` is the object generation conditions on; `AutopilotConfig`,
`AutopilotJob` and `AutopilotDraft` are the hands-off drafting loop layered on
top of it (Phase 12). They live here rather than in an app of their own because
design.md §5's repo layout puts them here — autopilot is a mode of operating a
product, not a separate domain.
"""

from __future__ import annotations

from typing import ClassVar

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class ProductFormat(models.TextChoices):
    IMAGE = "image", "Image"
    CAROUSEL = "carousel", "Carousel"
    VIDEO = "video", "Video"


class HashtagStyle(models.TextChoices):
    MINIMAL = "MINIMAL", "Minimal"
    MODERATE = "MODERATE", "Moderate"
    HEAVY = "HEAVY", "Heavy"


class EmojiStyle(models.TextChoices):
    NONE = "NONE", "None"
    LIGHT = "LIGHT", "Light"
    EXPRESSIVE = "EXPRESSIVE", "Expressive"


class Product(models.Model):
    """design.md §6.4. `reference_images` is the only hard requirement (I7) —
    every other field feeds the completeness scorer
    (`products/services/completeness.py`) but generation-readiness turns on
    references alone, recomputed by `products.services.products` on every m2m
    change rather than left as a derived property, so it stays queryable
    (`Product.objects.filter(is_generation_ready=True)`) the way a plain
    `@property` never would be.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="products"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # Free-form studio defaults (e.g. a preferred aspect ratio) — nothing here
    # feeds the completeness scorer or a hard constraint, unlike `restrictions`.
    preferences = models.JSONField(default=dict, blank=True)
    # A list of prompt constraints, not one blob of text: product-new.html's
    # "+ Add restriction" builds this up one hard rule at a time, and Phase 7
    # injects each as its own hard constraint (design.md §8.3).
    restrictions = models.JSONField(default=list, blank=True)
    category = models.ForeignKey(
        "categories.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )

    reference_images = models.ManyToManyField(
        "content.MediaAsset", related_name="products_referencing", blank=True
    )
    # Reserved, D7: trained per-product adapters (LoRA) are deferred, the image
    # provider does not support custom adapters yet. Never written to in v1.
    adapter_ref = models.CharField(max_length=128, blank=True)

    formats = models.JSONField(default=list, blank=True, help_text="ProductFormat values.")
    platforms = models.JSONField(default=list, blank=True, help_text="content.Platform values.")
    voice = models.CharField(max_length=300, blank=True)
    moods = models.JSONField(default=list, blank=True)
    hashtags_style = models.CharField(max_length=16, choices=HashtagStyle.choices, blank=True)
    emoji_style = models.CharField(max_length=16, choices=EmojiStyle.choices, blank=True)
    ctas = models.JSONField(default=list, blank=True)

    # Both server-controlled: `products.services.completeness.recompute_completeness`
    # is the only writer (implementation.md Phase 5.2).
    completeness_score = models.PositiveSmallIntegerField(default=0)
    is_generation_ready = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["workspace", "is_generation_ready"]),
        ]

    def __str__(self) -> str:
        return self.name


class AutopilotStrategy(models.TextChoices):
    TREND_LED = "TREND_LED", "Trend-led"
    EVERGREEN = "EVERGREEN", "Evergreen"
    PRODUCT_LED = "PRODUCT_LED", "Product-led"
    PROMOTIONAL = "PROMOTIONAL", "Promotional"


class AutopilotLanding(models.TextChoices):
    AUTO_CALENDAR = "AUTO_CALENDAR", "Straight to the calendar"
    REVIEW_QUEUE = "REVIEW_QUEUE", "Review queue"


class AutopilotConfig(models.Model):
    """design.md §6.4, §8.7. One per product — autopilot is a mode of operating
    a product, so there is nothing to configure twice.

    Every field here is a rhythm or a bias, never a quota: how often to draft
    (`cadence_days`), how far ahead (`lookahead_days`), what mix to draft
    (`strategy`/`strategy_weights`/`format_mix`) and how far from the product's
    established pattern the generator may wander (`latitude`). What the
    workspace is *allowed* to spend doing it stays on `Plan` (I8).
    """

    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="autopilot")
    enabled = models.BooleanField(default=False)

    # The posting rhythm, not the scan rhythm: Beat scans daily and fills every
    # unfilled slot on this grid out to the horizon
    # (`products/services/autopilot.py`).
    cadence_days = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(90)]
    )
    lookahead_days = models.PositiveSmallIntegerField(
        default=14, validators=[MinValueValidator(1), MaxValueValidator(365)]
    )

    strategy = models.CharField(
        max_length=16, choices=AutopilotStrategy.choices, default=AutopilotStrategy.PRODUCT_LED
    )
    # Optional per-strategy bias, e.g. {"TREND_LED": 2, "PRODUCT_LED": 1}. Empty
    # means `strategy` alone drives every slot.
    strategy_weights = models.JSONField(default=dict, blank=True)
    latitude = models.PositiveSmallIntegerField(
        default=40,
        validators=[MaxValueValidator(100)],
        help_text="0 stays on the product's established pattern; 100 wanders furthest from it.",
    )

    # Same weighted shape as `strategy_weights`, over ProductFormat values.
    format_mix = models.JSONField(default=dict, blank=True)
    platforms = models.JSONField(default=list, blank=True, help_text="content.Platform values.")

    landing = models.CharField(
        max_length=16, choices=AutopilotLanding.choices, default=AutopilotLanding.REVIEW_QUEUE
    )
    # Advanced only (§4.1). Gated on write by `PATCH /products/{id}/autopilot/`
    # and re-checked by the engine, because a plan can be downgraded between
    # the two.
    auto_approve = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"autopilot for {self.product_id}"


class AutopilotJobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    GENERATED = "GENERATED", "Generated"
    QUEUED = "QUEUED", "Queued for review"
    BLOCKED_QUOTA = "BLOCKED_QUOTA", "Blocked on quota"
    FAILED = "FAILED", "Failed"


class AutopilotJob(models.Model):
    """One cadence tick. Its existence is what marks the tick as consumed:
    `due_configs` asks when this config last ran rather than reading a
    `last_run_at` column, so the answer survives everything and cannot drift
    from the drafts it produced.

    A `BLOCKED_QUOTA` or `FAILED` row counts as a run for exactly that reason —
    without it, a config that cannot proceed would be retried on every scan.
    """

    config = models.ForeignKey(AutopilotConfig, on_delete=models.CASCADE, related_name="jobs")
    run_at = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=AutopilotJobStatus.choices, default=AutopilotJobStatus.PENDING
    )
    # Why it ended the way it did, and what it produced — the same
    # `error_detail` shape `Generation` uses, kept as data so the review queue
    # can explain a blocked run without the engine formatting prose.
    detail = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-run_at"]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["config", "-run_at"])]

    def __str__(self) -> str:
        return f"autopilot job {self.pk} ({self.status})"


class AutopilotDraftKind(models.TextChoices):
    IMAGE = "IMAGE", "Image"
    VIDEO = "VIDEO", "Video"


class AutopilotDraftStatus(models.TextChoices):
    """design.md §6.4's full enum. `PENDING`, `SCHEDULED` and `REJECTED` are
    reachable in Phase 12; `APPROVED` is the resting state for a draft whose
    post could not be scheduled, and `PUBLISHED` follows the post's own
    lifecycle — the same declare-the-enum, gate-the-transitions shape
    `PostStatus` used in Phase 4 (design.md A47).
    """

    PENDING = "PENDING", "Pending review"
    APPROVED = "APPROVED", "Approved"
    SCHEDULED = "SCHEDULED", "Scheduled"
    PUBLISHED = "PUBLISHED", "Published"
    REJECTED = "REJECTED", "Rejected"


class AutopilotDraft(models.Model):
    """One drafted post, waiting for its slot.

    `generation` is the *visual* — the one `kind` describes — and `caption`
    holds the separately generated text, which is why §4.2 prices an autopilot
    image (2 credits) and a text generation (1) rather than one combined figure.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="autopilot_drafts")
    generation = models.ForeignKey(
        "ai.Generation", on_delete=models.PROTECT, related_name="autopilot_drafts"
    )
    post = models.ForeignKey(
        "content.Post",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="autopilot_drafts",
    )
    kind = models.CharField(max_length=8, choices=AutopilotDraftKind.choices)
    platform = models.CharField(max_length=16, blank=True)
    caption = models.TextField(blank=True)
    scheduled_for = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=AutopilotDraftStatus.choices, default=AutopilotDraftStatus.PENDING
    )
    # Which strategy produced it, so the queue can say why this draft exists
    # and a later run can see the mix it has already laid down.
    strategy = models.CharField(max_length=16, choices=AutopilotStrategy.choices)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["scheduled_for", "id"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # What makes a re-run idempotent: the engine plans slots on a fixed
            # grid, so a second scan over the same horizon collides here rather
            # than drafting the same slot twice. It also means a *rejected*
            # slot is not silently re-drafted — the user said no to that slot,
            # not to that attempt.
            models.UniqueConstraint(
                fields=["product", "scheduled_for"], name="unique_autopilot_slot_per_product"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["product", "status"]),
        ]

    def __str__(self) -> str:
        return f"autopilot draft {self.pk} for {self.scheduled_for:%Y-%m-%d}"
