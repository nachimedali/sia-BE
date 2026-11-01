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
    """All optional: three of the tools take no input at all, and which
    is which is the client's business, not a per-slug serializer class."""

    topic = serializers.CharField(required=False, allow_blank=True, max_length=500)
    body = serializers.CharField(required=False, allow_blank=True, max_length=10_000)
    #: `caption-rewrite` sizes to it; `translate` takes `language`, `alt-text`
    #: an uploaded image. Each runner checks its own, as with `topic`/`body`.
    platform = serializers.CharField(required=False, allow_blank=True, max_length=32)
    language = serializers.CharField(required=False, allow_blank=True, max_length=8)
    media_id = serializers.IntegerField(required=False, min_value=1)


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
    credits_cost = serializers.IntegerField()
    used_this_month = serializers.IntegerField()


class ToolReadinessSerializer(serializers.Serializer[object]):
    ready = serializers.BooleanField()
    balance = serializers.IntegerField()
    #: Platform staff only: `ToolConfig` is global, so a customer admin's edit
    #: would reprice every other customer.
    can_edit_prices = serializers.BooleanField()
    requirements = RequirementSerializer(many=True)
    tools = ToolStateSerializer(many=True)


class ToolPriceSerializer(serializers.Serializer[object]):
    credits_cost = serializers.IntegerField(min_value=0)
