from __future__ import annotations

from django.contrib import admin

from playbook.models import PlaybookPost, PlaybookSource


@admin.register(PlaybookSource)
class PlaybookSourceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = [
        "label",
        "provider",
        "region",
        "category",
        "refresh_minutes",
        "is_active",
        "last_success_at",
        "consecutive_failures",
    ]
    list_filter = ["provider", "region", "is_active"]
    readonly_fields = [
        "last_run_at",
        "last_success_at",
        "last_error",
        "consecutive_failures",
    ]


@admin.register(PlaybookPost)
class PlaybookPostAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = [
        "title",
        "provider",
        "region",
        "format",
        "views",
        "trend_score",
        "posted_at",
        "is_fake",
    ]
    list_filter = ["provider", "region", "format", "is_fake"]
    search_fields = ["title", "creator", "external_id"]
