"""S21 — the Studio's own documents: projects (S21-01) and layers (S21-02)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ai.models import StudioProject
from ai.services import layers
from common.exceptions import OCCSError

#: The editor's state is the page's document, not a store for media: past
#: this it is a bug on the page (or a misuse), never a project.
STATE_MAX_BYTES = 256 * 1024


class ProjectTooLargeError(OCCSError):
    default_code = "project_too_large"
    default_detail = "This project is too large to save."


class StudioProjectSerializer(serializers.ModelSerializer[StudioProject]):
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = StudioProject
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "title",
            "kind",
            "state",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "created_by",
            "created_at",
            "updated_at",
        )

    def get_created_by(self, obj: StudioProject) -> str:
        return obj.created_by.email if obj.created_by else ""

    def validate_state(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise serializers.ValidationError("The state is an object.")
        if len(json.dumps(value, separators=(",", ":"))) > STATE_MAX_BYTES:
            raise ProjectTooLargeError(detail={"limit": STATE_MAX_BYTES})
        return value


class StudioProjectCardSerializer(serializers.ModelSerializer[StudioProject]):
    """The start page's card: no editor state, only the page's own summary."""

    summary = serializers.SerializerMethodField()

    class Meta:
        model = StudioProject
        fields: ClassVar[tuple[str, ...]] = ("id", "title", "kind", "summary", "updated_at")
        read_only_fields = fields

    def get_summary(self, obj: StudioProject) -> dict[str, Any]:
        # Annotated by the list's queryset (the state itself is deferred).
        summary = getattr(obj, "summary", None)
        return summary if isinstance(summary, dict) else {}


class LayerSerializer(serializers.Serializer[Any]):
    """One drawn layer (`ai.services.layers`); checked there, described here."""

    kind = serializers.ChoiceField(choices=sorted(layers.KINDS))
    text = serializers.CharField(max_length=layers.TEXT_MAX, allow_blank=True)
    name = serializers.CharField(max_length=layers.NAME_MAX, allow_blank=True, required=False)
    side = serializers.ChoiceField(choices=list(layers.SIDES), required=False)
    style = serializers.ChoiceField(  # type: ignore[assignment]
        choices=list(layers.CAPTION_STYLES), required=False
    )
    x = serializers.FloatField()
    y = serializers.FloatField()
    w = serializers.FloatField()
    start = serializers.FloatField()
    end = serializers.FloatField(allow_null=True, required=False)


class LayerKitSerializer(serializers.Serializer[Any]):
    primary = serializers.CharField(max_length=7)
    accent = serializers.CharField(max_length=7)
    logo = serializers.BooleanField()


@extend_schema_field(LayerSerializer(many=True))
class _LayersField(serializers.JSONField):
    """Raw: `ai.services.layers.clean` checks each layer and names the field."""


@extend_schema_field(LayerKitSerializer)
class _KitField(serializers.JSONField):
    pass


class VideoLayersSerializer(serializers.Serializer[Any]):
    layers = _LayersField()
    kit = _KitField(required=False, default=dict)


class StillLayersSerializer(VideoLayersSerializer):
    media = serializers.IntegerField()
    aspect = serializers.CharField(max_length=8)


class StillSendSerializer(serializers.Serializer[Any]):
    media = serializers.IntegerField()
    #: The post's caption: the ad's words, as the page shows them.
    text = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    language = serializers.CharField(max_length=8, required=False, allow_blank=True, default="")
    platforms = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False, default=list
    )


class StillSendResponseSerializer(serializers.Serializer[Any]):
    post = serializers.IntegerField()
