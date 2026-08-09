"""Product serialisation (design.md §7).

Reuses `content.serializers.MediaAssetSerializer` for `reference_images`
rather than defining a parallel one — a reference image is just a
`MediaAsset`, viewed the same way `/media/` already renders it.
"""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from content.models import Platform
from content.serializers import MediaAssetSerializer
from products.models import Product, ProductFormat


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
