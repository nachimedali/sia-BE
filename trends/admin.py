"""Admin for the trend corpus.

`TrendSource` is the only editable table here: which vendors are asked, for
which category, is an operational decision that must not need a deploy (D12).
Items and clusters are pipeline output — read-only, because editing a score by
hand would put the corpus out of step with the formula that produced it, and
the honest way to change what they say is to re-run the extraction.
"""

from __future__ import annotations

from django.contrib import admin

from common.admin import all_fields_except_id
from trends.models import TrendCluster, TrendItem, TrendSource


@admin.register(TrendSource)
class TrendSourceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("category", "platform", "kind", "vendor", "is_active", "watermark_at")
    list_filter = ("kind", "platform", "is_active")
    search_fields = ("category__name", "vendor")
    autocomplete_fields = ("category",)
    readonly_fields = ("watermark_at", "created_at", "updated_at")


@admin.register(TrendItem)
class TrendItemAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("external_id", "source", "modality", "posted_at", "composite_score")
    list_filter = ("modality", "excluded_reason", "source__platform")
    search_fields = ("external_id", "author_handle", "body")
    readonly_fields = all_fields_except_id(TrendItem)

    def has_add_permission(self, request: object) -> bool:
        return False


@admin.register(TrendCluster)
class TrendClusterAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("label", "category", "platform", "item_count", "composite_score", "expires_at")
    list_filter = ("platform",)
    search_fields = ("label", "category__name")
    readonly_fields = all_fields_except_id(TrendCluster)

    def has_add_permission(self, request: object) -> bool:
        return False
