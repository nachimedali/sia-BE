from __future__ import annotations

from typing import Any

from django.contrib import admin

from content.models import MediaAsset, Post, PostMediaAttachment, PostTarget
from reminders.models import Reminder


class PostMediaAttachmentInline(admin.TabularInline):  # type: ignore[type-arg]
    model = PostMediaAttachment
    extra = 0
    autocomplete_fields = ("media_asset",)


class PostTargetInline(admin.TabularInline):  # type: ignore[type-arg]
    model = PostTarget
    extra = 0
    readonly_fields = ("provider_post_id", "platform_post_id", "published_at", "attempt_count")


class ReminderInline(admin.TabularInline):  # type: ignore[type-arg]
    model = Reminder
    extra = 0
    fields = ("state", "send_at", "sent_at", "confirmed_at", "snoozed_to")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        # Rows come only from `scheduling.services.schedule_post` /
        # `reminders.services` — an admin-created one would skip the arm/send
        # lifecycle those own.
        return False


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("__str__", "workspace", "status", "source", "scheduled_at", "created_at")
    list_filter = ("status", "source", "delivery_mode")
    search_fields = ("master_body", "workspace__name")
    autocomplete_fields = ("workspace", "author", "category", "origin_post")
    inlines = (PostMediaAttachmentInline, PostTargetInline, ReminderInline)


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("__str__", "workspace", "kind", "source", "mime", "created_at")
    list_filter = ("kind", "source")
    search_fields = ("workspace__name", "checksum")
    autocomplete_fields = ("workspace",)
    readonly_fields = ("checksum", "width", "height", "duration_ms", "mime")
