"""Product serialisation (design.md §7).

Reuses `content.serializers.MediaAssetSerializer` for `reference_images`
rather than defining a parallel one — a reference image is just a
`MediaAsset`, viewed the same way `/media/` already renders it.
"""

from __future__ import annotations

from typing import ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from content.models import Platform
from content.serializers import MediaAssetSerializer
from products.models import AutopilotConfig, AutopilotDraft, Product, ProductFormat


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
            "completeness_score",
            "is_generation_ready",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "reference_images",
            "completeness_score",
            "is_generation_ready",
            "created_at",
            "updated_at",
        )


class ProductReferenceImagesUploadSerializer(serializers.Serializer[object]):
    """Documents the multipart upload shape for the schema. The view reads
    `request.FILES` directly, same reasoning as `MediaAssetUploadSerializer`."""

    files = serializers.ListField(child=serializers.FileField())


class ProductCompletenessMissingSerializer(serializers.Serializer[object]):
    key = serializers.CharField()
    message = serializers.CharField()
    impact = serializers.IntegerField()


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
