"""Content serialisation (design.md §7).

`PostSerializer`'s write surface is deliberately narrow (design.md A49):
`status`, `delivery_mode`, `scheduled_at`, `source` and `origin_post` are all
system-controlled until the phases that own their transitions — scheduling
(Phase 8), publishing (Phase 9), repurposing (Phase 11) — land.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.utils import timezone
from rest_framework import serializers
from rest_framework.request import Request

from common.workspaces import scope_related_field_to_workspace
from content.models import DeliveryMode, MediaAsset, Platform, Post


def _scope_media_field(field: Any, request: Request | None) -> None:
    """Restricts a `media_asset_ids` field's choices to the caller's own
    workspace — a bare model-wide queryset would let one workspace reference
    another's media."""
    scope_related_field_to_workspace(field, request, MediaAsset)


class MediaAssetSerializer(serializers.ModelSerializer[MediaAsset]):
    url = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "kind",
            "url",
            "mime",
            "width",
            "height",
            "duration_ms",
            "checksum",
            "source",
            "created_at",
        )
        read_only_fields = fields

    def get_url(self, obj: MediaAsset) -> str | None:
        return obj.file.url if obj.file else None


class MediaAssetUploadSerializer(serializers.Serializer[Any]):
    """Documents the multipart upload shape for the schema. The view reads
    `request.FILES` directly — a `FileField` here has nowhere to attach the
    workspace before `ingest_media` needs it."""

    file = serializers.FileField()


class PostMediaAssetSerializer(MediaAssetSerializer):
    """Nested, ordered view of a post's attached media (`Post.ordered_media`).

    A narrower `MediaAssetSerializer`, not a parallel definition — same `url`
    field and `get_url()`, fewer columns exposed.
    """

    class Meta(MediaAssetSerializer.Meta):
        fields: ClassVar[tuple[str, ...]] = ("id", "kind", "url")
        read_only_fields = fields

    def get_url(self, obj: MediaAsset) -> str | None:
        return obj.file.url if obj.file else None


class PostSerializer(serializers.ModelSerializer[Post]):
    media = PostMediaAssetSerializer(source="ordered_media", many=True, read_only=True)
    media_asset_ids = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.none(), many=True, write_only=True, required=False
    )

    class Meta:
        model = Post
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "master_body",
            "media",
            "media_asset_ids",
            "status",
            "delivery_mode",
            "scheduled_at",
            "source",
            "category",
            "origin_post",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "status",
            "delivery_mode",
            "scheduled_at",
            "source",
            "origin_post",
            "created_at",
            "updated_at",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _scope_media_field(self.fields["media_asset_ids"], self.context.get("request"))


class PostPreviewRequestSerializer(serializers.Serializer[Any]):
    master_body = serializers.CharField(allow_blank=True, default="")
    media_asset_ids = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.none(), many=True, required=False, default=list
    )
    platforms = serializers.ListField(
        child=serializers.ChoiceField(choices=Platform.choices), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _scope_media_field(self.fields["media_asset_ids"], self.context.get("request"))


class PostScheduleRequestSerializer(serializers.Serializer[Any]):
    """`POST /posts/{id}/schedule/` (implementation.md Phase 8). Shape only —
    the horizon check against `Plan.scheduling_horizon_days` is an
    entitlement, not a validation rule, so it lives in
    `scheduling.services.schedule_post`, not here (design.md A2: 402 is
    reserved for entitlement failures)."""

    delivery_mode = serializers.ChoiceField(choices=DeliveryMode.choices)
    scheduled_at = serializers.DateTimeField()

    def validate_scheduled_at(self, value: Any) -> Any:
        if value <= timezone.now():
            raise serializers.ValidationError("scheduled_at must be in the future.")
        return value


class AdaptedMediaSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    kind = serializers.CharField()
    url = serializers.CharField()


class AdaptedPayloadSerializer(serializers.Serializer[Any]):
    platform = serializers.CharField()
    body = serializers.CharField()
    thread = serializers.ListField(child=serializers.CharField())
    hashtags = serializers.ListField(child=serializers.CharField())
    media = AdaptedMediaSerializer(many=True)
    truncated = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())


class PostPreviewResponseSerializer(serializers.Serializer[Any]):
    payloads = serializers.DictField(child=AdaptedPayloadSerializer())
