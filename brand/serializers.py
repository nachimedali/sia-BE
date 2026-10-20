"""Wire shapes for the brand import and the Brand Core."""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from brand.models import SECTIONS, BrandCore, BrandImport, ProductImport


class BrandImportSerializer(serializers.ModelSerializer[BrandImport]):
    pages_read = serializers.SerializerMethodField()

    class Meta:
        model = BrandImport
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "url",
            "domain",
            "status",
            "stage",
            "progress",
            "pages",
            "pages_read",
            "result",
            "review",
            "edits",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        )
        read_only_fields = fields

    def get_pages_read(self, obj: BrandImport) -> int:
        return len(obj.pages or [])


class BrandImportStartSerializer(serializers.Serializer[Any]):
    url = serializers.CharField(max_length=500)


class BrandImportReviewSerializer(serializers.Serializer[Any]):
    section = serializers.ChoiceField(choices=list(SECTIONS))
    action = serializers.ChoiceField(choices=["accept", "edit"])
    value = serializers.DictField(required=False)


class BrandCoreSerializer(serializers.ModelSerializer[BrandCore]):
    class Meta:
        model = BrandCore
        fields: ClassVar[tuple[str, ...]] = (
            "version",
            "sections",
            "identity",
            "source_import",
            "created_at",
        )
        read_only_fields = fields


class BrandChangeSerializer(serializers.Serializer[Any]):
    section = serializers.CharField()
    field = serializers.CharField()
    kind = serializers.ChoiceField(choices=["added", "removed", "changed"])
    before = serializers.CharField(allow_blank=True)
    after = serializers.CharField(allow_blank=True)


class BrandSkippedSerializer(serializers.Serializer[Any]):
    name = serializers.CharField()
    reason = serializers.CharField()


class BrandWroteSerializer(serializers.Serializer[Any]):
    """What the accepted sections wrote beyond the kit."""

    products_created = serializers.ListField(child=serializers.CharField())
    products_skipped = BrandSkippedSerializer(many=True)
    taste_version = serializers.IntegerField(allow_null=True)
    taste_activated = serializers.BooleanField()
    competitors_tracked = serializers.ListField(child=serializers.CharField())
    competitors_skipped = BrandSkippedSerializer(many=True)


class BrandApplyResponseSerializer(serializers.Serializer[Any]):
    core = BrandCoreSerializer()
    kept = serializers.ListField(child=serializers.CharField())
    filled = serializers.ListField(child=serializers.CharField())
    changes = BrandChangeSerializer(many=True)
    wrote = BrandWroteSerializer()


class ProductImportSerializer(serializers.ModelSerializer[ProductImport]):
    class Meta:
        model = ProductImport
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "url",
            "domain",
            "status",
            "stage",
            "progress",
            "pages",
            "result",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        )
        read_only_fields = fields
