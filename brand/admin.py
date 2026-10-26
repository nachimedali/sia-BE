"""Brand admin: avatars and their portrait batches (S17), read-only.

An avatar changes only through `brand.services.avatars` — consent and its
withdrawal must land in the audit log, which an admin edit would skip.
"""

from __future__ import annotations

from django.contrib import admin

from brand.models import Avatar, AvatarDraft
from common.admin import ReadOnlyAdmin


@admin.register(Avatar)
class AvatarAdmin(ReadOnlyAdmin):
    list_display = (
        "name",
        "workspace",
        "kind",
        "likeness",
        "is_house",
        "created_at",
        "archived_at",
    )
    list_filter = ("kind", "likeness", "is_house")
    search_fields = ("name", "workspace__name")


@admin.register(AvatarDraft)
class AvatarDraftAdmin(ReadOnlyAdmin):
    list_display = ("id", "workspace", "status", "credits", "created_at")
    list_filter = ("status",)
