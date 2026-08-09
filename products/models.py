"""Products (design.md §6.4, D7). The object generation conditions on.

`AutopilotConfig`/`AutopilotJob`/`AutopilotDraft` — the other three models
§6.4 lists — are deliberately absent here, the same deferred-app shape Phase 4
used for `Post.product`/`Post.generation` (design.md A48): autopilot does not
exist until Phase 12, and a `Product` needs no forward reference to it.
"""

from __future__ import annotations

from typing import ClassVar

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
