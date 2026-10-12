from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from ai.services import references
from playbook.models import PlaybookPost, PostFormat, Provider

CAPTION_PREVIEW = 400


class PlaybookCategorySerializer(serializers.Serializer[Any]):
    slug = serializers.CharField()
    name = serializers.CharField()


class NewsStorySerializer(serializers.Serializer[Any]):
    title = serializers.CharField()
    url = serializers.CharField(allow_blank=True)
    #: Stored as `source`; renamed on the wire because `source` is a DRF
    #: field's own attribute.
    outlet = serializers.CharField(source="source", allow_blank=True)


class PlaybookPostSerializer(serializers.ModelSerializer[PlaybookPost]):
    provider_label = serializers.CharField(source="get_provider_display", read_only=True)
    category = PlaybookCategorySerializer(read_only=True, allow_null=True)
    caption = serializers.SerializerMethodField()
    can_guide_pictures = serializers.BooleanField(read_only=True)
    hashtags = serializers.ListField(child=serializers.CharField(), read_only=True)
    news = NewsStorySerializer(many=True, read_only=True)
    structure = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = PlaybookPost
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "provider",
            "provider_label",
            "format",
            "url",
            "thumbnail_url",
            "thumbnail_credit",
            "title",
            "caption",
            "creator",
            "hashtags",
            "duration_seconds",
            "posted_at",
            "views",
            "likes",
            "comments",
            "traffic_label",
            "news",
            "engagement",
            "trend_score",
            "structure",
            "category",
            "can_guide_pictures",
            "is_fake",
        )
        read_only_fields = fields

    def get_caption(self, row: PlaybookPost) -> str:
        return row.caption[:CAPTION_PREVIEW]


class CategoryFacetSerializer(serializers.Serializer[Any]):
    slug = serializers.CharField()
    name = serializers.CharField()
    count = serializers.IntegerField()


class FacetsSerializer(serializers.Serializer[Any]):
    category = CategoryFacetSerializer(many=True)
    platform = serializers.DictField(child=serializers.IntegerField())
    format = serializers.DictField(child=serializers.IntegerField())
    posted = serializers.DictField(child=serializers.IntegerField())


class PlaybookWallSerializer(serializers.Serializer[Any]):
    region = serializers.CharField()
    market = serializers.CharField(allow_blank=True)
    count = serializers.IntegerField()
    page = serializers.IntegerField()
    pages = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total = serializers.IntegerField()
    refreshed_at = serializers.DateTimeField(allow_null=True)
    sample = serializers.BooleanField()
    results = PlaybookPostSerializer(many=True)
    facets = FacetsSerializer()


class ReferencePickSerializer(serializers.Serializer[Any]):
    post = serializers.IntegerField(min_value=1)
    note = serializers.CharField(
        allow_blank=True, default="", max_length=references.NOTE_MAX, trim_whitespace=True
    )
    take = serializers.ListField(
        child=serializers.ChoiceField(choices=list(references.ASPECTS)), default=list
    )
    influence = serializers.ChoiceField(choices=list(references.INFLUENCE), default="balanced")


class ComposeRequestSerializer(serializers.Serializer[Any]):
    references = ReferencePickSerializer(many=True)
    product = serializers.IntegerField(min_value=1)
    output = serializers.ChoiceField(choices=["image", "video"], default="image")
    #: A `format` row of kind IMAGE. Ignored for video, which renders 9:16
    #: stills for the Studio's Motion step to animate.
    format = serializers.CharField(allow_blank=True, default="")
    variants = serializers.IntegerField(min_value=1, max_value=6, default=4)
    language = serializers.CharField(allow_blank=True, default="")
    moods = serializers.ListField(child=serializers.CharField(), default=list)
    platforms = serializers.ListField(child=serializers.CharField(), default=list)
    brief = serializers.CharField(allow_blank=True, default="", max_length=600)

    def validate_references(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not 1 <= len(value) <= references.MAX_REFERENCES:
            raise serializers.ValidationError(
                f"Pick one to {references.MAX_REFERENCES} references."
            )
        ids = [pick["post"] for pick in value]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("A reference is listed twice.")
        return value


#: The vocabulary the page needs, so it never restates the server's lists.
class PlaybookVocabularySerializer(serializers.Serializer[Any]):
    providers = serializers.DictField(child=serializers.CharField())
    formats = serializers.DictField(child=serializers.CharField())
    aspects = serializers.DictField(child=serializers.CharField())
    influences = serializers.DictField(child=serializers.CharField())
    max_references = serializers.IntegerField()


def vocabulary() -> dict[str, Any]:
    return {
        "providers": dict(Provider.choices),
        "formats": dict(PostFormat.choices),
        "aspects": {key: label for key, (label, _phrase) in references.ASPECTS.items()},
        "influences": {"light": "Light touch", "balanced": "Balanced", "strong": "Strong"},
        "max_references": references.MAX_REFERENCES,
    }
