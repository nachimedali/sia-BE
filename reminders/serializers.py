"""Reminder serialisation (design.md §7, §8.5).

`ReminderPacketSerializer` is read by the public `/r/[token]` views — nothing
workspace-identifying goes in it beyond what the packet itself needs to
render (design.md: the token "must expose nothing beyond that post's
packet").
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from content.models import Platform
from content.serializers import AdaptedPayloadSerializer, PostMediaAssetSerializer
from content.services.adaptation import render_post
from reminders.models import Reminder

# Best-effort deep links into each platform's own composer — not an OCCS API,
# just where that app's own "create post" screen tends to live. A dead link
# degrades to "open the app," never a broken page.
_COMPOSER_LINKS: dict[str, str] = {
    Platform.INSTAGRAM: "instagram://camera",
    Platform.TIKTOK: "https://www.tiktok.com/upload",
    Platform.LINKEDIN: "https://www.linkedin.com/feed/?shareActive=true",
    Platform.YOUTUBE: "https://studio.youtube.com/channel/upload",
    Platform.THREADS: "https://www.threads.net/",
    Platform.FACEBOOK: "fb://composer",
}


class ReminderPacketSerializer(serializers.Serializer[Reminder]):
    id = serializers.IntegerField()
    state = serializers.CharField()
    scheduled_at = serializers.DateTimeField(source="post.scheduled_at")
    confirmed_at = serializers.DateTimeField(allow_null=True)
    media = PostMediaAssetSerializer(source="post.ordered_media", many=True)
    payloads = serializers.SerializerMethodField()
    composer_links = serializers.SerializerMethodField()
    # Stubbed until Phase 10's trend engine exists to supply one — the same
    # deferred shape `ai/services/prompting.py` used for category-signal
    # grounding ahead of Phase 10 (design.md A70).
    recommended_sound = serializers.SerializerMethodField()

    @extend_schema_field(serializers.DictField(child=AdaptedPayloadSerializer()))
    def get_payloads(self, reminder: Reminder) -> dict[str, Any]:
        post = reminder.post
        platforms = [p for p in post.workspace.platforms if p in Platform.values]
        payloads = render_post(post, platforms or list(Platform.values))
        return {platform: payload.as_dict() for platform, payload in payloads.items()}

    def get_composer_links(self, reminder: Reminder) -> dict[str, str]:
        platforms = [p for p in reminder.post.workspace.platforms if p in Platform.values]
        return {p: _COMPOSER_LINKS[p] for p in platforms if p in _COMPOSER_LINKS}

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_recommended_sound(self, reminder: Reminder) -> None:
        return None


class ReminderSerializer(serializers.ModelSerializer[Reminder]):
    post_id = serializers.IntegerField(source="post.id", read_only=True)
    master_body = serializers.CharField(source="post.master_body", read_only=True)

    class Meta:
        model = Reminder
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post_id",
            "master_body",
            "channel",
            "send_at",
            "sent_at",
            "confirmed_at",
            "snoozed_to",
            "state",
        )
        read_only_fields = fields


class ReminderSnoozeRequestSerializer(serializers.Serializer[Any]):
    snoozed_to = serializers.DateTimeField()

    def validate_snoozed_to(self, value: Any) -> Any:
        if value <= timezone.now():
            raise serializers.ValidationError("snoozed_to must be in the future.")
        return value
