"""Notification serialisation (P2-12)."""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from notifications.models import (
    DEFAULT_TRANSPORTS,
    EventKey,
    Notification,
    NotificationPreference,
    Transport,
)


class NotificationSerializer(serializers.ModelSerializer[Notification]):
    class Meta:
        model = Notification
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "event_key",
            "payload",
            "read_at",
            "created_at",
        )
        read_only_fields = fields


class MarkReadRequestSerializer(serializers.Serializer[Any]):
    """Omit `ids` to clear everything unread in this workspace — the "mark all
    read" the bell needs. An empty list is **not** the same as omitting it: it
    marks nothing, which is what a client sending a filtered-to-nothing
    selection means."""

    ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class NotificationPreferenceSerializer(serializers.ModelSerializer[NotificationPreference]):
    class Meta:
        model = NotificationPreference
        fields: ClassVar[tuple[str, ...]] = ("event_key", "transports")

    def validate_transports(self, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(Transport.values))
        if unknown:
            raise serializers.ValidationError(f"Unknown transports: {', '.join(unknown)}.")
        return sorted(set(value))


def preference_rows(stored: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Every event, with the stored choice or the default.

    The **whole** set rather than only what has been saved: a settings screen
    has to render a row for each event, and a client filling the gaps itself
    would be a second copy of `DEFAULT_TRANSPORTS` to drift from this one.
    """
    return [
        {
            "event_key": key,
            "label": EventKey(key).label,
            "transports": stored.get(key, list(default)),
            "is_default": key not in stored,
        }
        for key, default in DEFAULT_TRANSPORTS.items()
    ]
