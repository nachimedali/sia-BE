"""Product serialisation (design.md §7).

Reuses `content.serializers.MediaAssetSerializer` for `reference_images`
rather than defining a parallel one — a reference image is just a
`MediaAsset`, viewed the same way `/media/` already renders it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.setup import RequirementSerializer
from common.workspaces import request_workspace
from content.models import Platform, PostStatus
from content.serializers import MediaAssetSerializer
from products import brief
from products.models import AutopilotConfig, AutopilotDraft, Product, ProductFormat
from taste.models import REASON_CODES


class ProductSerializer(serializers.ModelSerializer[Product]):
    reference_images = MediaAssetSerializer(many=True, read_only=True)
    formats = serializers.ListField(
        child=serializers.ChoiceField(choices=ProductFormat.choices), required=False
    )
    platforms = serializers.ListField(
        child=serializers.ChoiceField(choices=Platform.choices), required=False
    )
    restrictions = serializers.ListField(
        child=serializers.CharField(max_length=300), required=False
    )
    moods = serializers.ListField(child=serializers.CharField(max_length=100), required=False)
    ctas = serializers.ListField(child=serializers.CharField(max_length=100), required=False)
    # The brief (`products.brief`). Typed here so the generated client gets real
    # shapes rather than `unknown`; the catalog-key checks are `validate`'s.
    price = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=0, required=False, allow_null=True
    )
    features = serializers.ListField(child=serializers.CharField(), required=False)
    audience = serializers.ListField(child=serializers.CharField(), required=False)
    languages = serializers.ListField(child=serializers.CharField(), required=False)
    tone = serializers.DictField(child=serializers.IntegerField(), required=False)
    use_words = serializers.ListField(child=serializers.CharField(), required=False)
    avoid_words = serializers.ListField(child=serializers.CharField(), required=False)
    scenes = serializers.ListField(child=serializers.CharField(), required=False)
    lights = serializers.ListField(child=serializers.CharField(), required=False)
    brand_colors = serializers.ListField(child=serializers.CharField(), required=False)
    aspects = serializers.ListField(child=serializers.CharField(), required=False)
    must_include = serializers.ListField(child=serializers.CharField(), required=False)
    claims = serializers.ListField(child=serializers.DictField(), required=False)
    photo_policy = serializers.DictField(child=serializers.BooleanField(), required=False)
    reference_tags = serializers.DictField(child=serializers.CharField(), required=False)
    platform_plan = serializers.DictField(child=serializers.DictField(), required=False)
    brand_hashtags = serializers.ListField(child=serializers.CharField(), required=False)
    rules_reviewed = serializers.BooleanField(write_only=True, required=False)
    post_count = serializers.SerializerMethodField()
    published_count = serializers.SerializerMethodField()
    autopilot_enabled = serializers.SerializerMethodField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        request = self.context.get("request")
        workspace_id = request_workspace(request).pk if request is not None else None
        return brief.validate(attrs, workspace_id=workspace_id, instance=self.instance)

    class Meta:
        model = Product
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "description",
            "preferences",
            "restrictions",
            "category",
            "reference_images",
            "formats",
            "platforms",
            "voice",
            "moods",
            "hashtags_style",
            "emoji_style",
            "ctas",
            "sku",
            "price",
            "price_currency",
            "product_url",
            "short_description",
            "features",
            "audience",
            "languages",
            "tone",
            "tone_preset",
            "use_words",
            "avoid_words",
            "caption_length",
            "scenes",
            "lights",
            "people",
            "brand_colors",
            "aspects",
            "must_include",
            "claims",
            "ramadan_quiet_hours",
            "no_children",
            "mention_price",
            "approval_mode",
            "legal_mention",
            "rules_reviewed_at",
            "rules_reviewed",
            "photo_policy",
            "reference_tags",
            "platform_plan",
            "campaign_goal",
            "campaign_starts",
            "campaign_ends",
            "brand_hashtags",
            "completeness_score",
            "is_generation_ready",
            "post_count",
            "published_count",
            "autopilot_enabled",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "reference_images",
            "rules_reviewed_at",
            "completeness_score",
            "is_generation_ready",
            "created_at",
            "updated_at",
        )

    # The list/retrieve queryset annotates these. Create and update hand back
    # the service's own instance, which has no annotation, so count directly.
    def get_post_count(self, obj: Product) -> int:
        annotated = getattr(obj, "post_count", None)
        return annotated if annotated is not None else obj.posts.count()

    def get_published_count(self, obj: Product) -> int:
        annotated = getattr(obj, "published_count", None)
        if annotated is not None:
            return int(annotated)
        return obj.posts.filter(status=PostStatus.PUBLISHED).count()

    def get_autopilot_enabled(self, obj: Product) -> bool:
        # No config row is autopilot never switched on — off, not unknown.
        try:
            return obj.autopilot.enabled
        except AutopilotConfig.DoesNotExist:
            return False


class ProductReferenceImagesUploadSerializer(serializers.Serializer[object]):
    """Documents the multipart upload shape for the schema. The view reads
    `request.FILES` directly, same reasoning as `MediaAssetUploadSerializer`."""

    files = serializers.ListField(child=serializers.FileField())
    #: Optional, aligned to `files`: the shot type of each (a `shot_tag` key).
    tags = serializers.ListField(child=serializers.CharField(allow_blank=True), required=False)


class ProductCompletenessMissingSerializer(serializers.Serializer[object]):
    key = serializers.CharField()
    message = serializers.CharField()
    impact = serializers.IntegerField()


class CompletenessCheckSerializer(serializers.Serializer[object]):
    """One row of the scorer's definition — what the form's live panel renders
    its weights and required flags from (`GET /products/completeness-checks/`)."""

    # `text` / `is_required`, not `label` / `required`: those are attributes of
    # every DRF field, and declaring them shadows the framework's own.
    key = serializers.CharField()
    text = serializers.CharField()
    weight = serializers.IntegerField()
    is_required = serializers.BooleanField()
    section = serializers.CharField()


class ProductCompletenessSerializer(serializers.Serializer[object]):
    completeness_score = serializers.IntegerField()
    is_generation_ready = serializers.BooleanField()
    missing = ProductCompletenessMissingSerializer(many=True)


class AutopilotConfigSerializer(serializers.ModelSerializer[AutopilotConfig]):
    """design.md §6.4. `product` is read-only: the config is reached *through*
    its product (`/products/{id}/autopilot/`), so letting the body name a
    different one would be a second, unscoped way to address it."""

    platforms = serializers.ListField(
        child=serializers.ChoiceField(choices=Platform.choices), required=False
    )
    # Typed rather than left as a bare `JSONField`: both are weight maps, and an
    # untyped one reaches the generated client as `unknown`, which forces the
    # form to assert a shape the schema never promised. Declaring them also
    # rejects a malformed body at the edge — the engine's own tolerance for a
    # bad weight is for what an operator types into admin, not for the API.
    strategy_weights = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=False
    )
    format_mix = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False)

    class Meta:
        model = AutopilotConfig
        fields: ClassVar[tuple[str, ...]] = (
            "product",
            "enabled",
            "cadence_days",
            "lookahead_days",
            "strategy",
            "strategy_weights",
            "latitude",
            "format_mix",
            "platforms",
            "landing",
            "auto_approve",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("product", "created_at", "updated_at")


class AutopilotDraftSerializer(serializers.ModelSerializer[AutopilotDraft]):
    """What the review queue renders. `media` is flattened off the visual
    generation's first variant rather than nesting the whole `Generation`: the
    queue needs the picture, not the pipeline's working state."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    media = serializers.SerializerMethodField()

    class Meta:
        model = AutopilotDraft
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "product",
            "product_name",
            "generation",
            "post",
            "kind",
            "platform",
            "caption",
            "media",
            "scheduled_for",
            "status",
            "strategy",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(MediaAssetSerializer(allow_null=True))
    def get_media(self, obj: AutopilotDraft) -> dict[str, object] | None:
        variant = obj.generation.variants.first()
        if variant is None or variant.media_asset is None:
            return None
        return dict(MediaAssetSerializer(variant.media_asset).data)


class DraftRejectRequestSerializer(serializers.Serializer[Any]):
    """Why this draft was refused (P5-12).

    Defaults to `other` rather than being required: a reviewer clearing a
    queue should not be blocked on picking a taxonomy entry, and an
    unclassified rejection is still worth more than a deleted row. The default
    is deliberately the least informative code, so "they did not say" is
    visible in the aggregate rather than disguised as a real reason.
    """

    reason_code = serializers.ChoiceField(choices=REASON_CODES, default="other")


class AutopilotReadinessSerializer(serializers.Serializer[object]):
    """`/autopilot/readiness/`'s envelope (X-08).

    `next_slots` rather than a "next run" timestamp: the daily scan time is an
    operational detail that would read as a promise, while the slots are what
    the user actually asked about — the dates their calendar is about to fill.
    """

    ready = serializers.BooleanField()
    requirements = RequirementSerializer(many=True)
    next_slots = serializers.ListField(child=serializers.DateTimeField())
