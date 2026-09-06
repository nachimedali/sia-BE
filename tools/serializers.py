"""Tool serialisation (design.md §8.10)."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

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
