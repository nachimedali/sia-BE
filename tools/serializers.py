"""Tool serialisation (design.md §8.10)."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from common.setup import RequirementSerializer
from tools.models import ToolConfig, ToolUsage


class ToolConfigSerializer(serializers.ModelSerializer[ToolConfig]):
    """Reference data: what exists, whether it is on, and what it costs.

    No `description` field. Display copy for six fixed slugs belongs on the
    client that renders it, not on a backend row nothing else reads — and the
    frontend already holds it.
    """

    class Meta:
        model = ToolConfig
        fields = ("slug", "display_name", "is_enabled", "credits_cost")
        read_only_fields = fields


class ToolRunSerializer(serializers.Serializer[object]):
    """Both optional: three of the six tools take no input at all, and which
    is which is the client's business, not a per-slug serializer class."""

    topic = serializers.CharField(required=False, allow_blank=True, max_length=500)
    body = serializers.CharField(required=False, allow_blank=True, max_length=10_000)


class ToolUsageSerializer(serializers.ModelSerializer[ToolUsage]):
    class Meta:
        model = ToolUsage
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "tool",
            "output",
            "credits_charged",
            "created_at",
        )
        read_only_fields = fields


class ToolStateSerializer(serializers.Serializer[object]):
    """One tool's own verdict. `reason` is a key (`no_corpus`, `no_captures`),
    blank when the tool is ready — the wording belongs to whoever renders the
    card, like `display_name`'s absence of a `description` beside it."""

    slug = serializers.CharField()
    status = serializers.ChoiceField(choices=["ready", "blocked"])
    reason = serializers.CharField(allow_blank=True)


class ToolReadinessSerializer(serializers.Serializer[object]):
    ready = serializers.BooleanField()
    requirements = RequirementSerializer(many=True)
    tools = ToolStateSerializer(many=True)
