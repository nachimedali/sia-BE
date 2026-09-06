from __future__ import annotations

from django.contrib import admin

from common.admin import ReadOnlyAdmin, all_fields_except_id
from tools.models import ToolConfig, ToolUsage


@admin.register(ToolConfig)
class ToolConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Editable: `credits_cost` is a commercial number and `is_enabled` lets an
    operator take one tool down without a deploy and without the other five
    going with it."""

    list_display = ("slug", "display_name", "is_enabled", "credits_cost", "sort_order")
    list_editable = ("is_enabled", "credits_cost", "sort_order")


@admin.register(ToolUsage)
class ToolUsageAdmin(ReadOnlyAdmin):
    list_display = ("tool", "workspace", "credits_charged", "created_at")
    list_filter = ("tool",)
    readonly_fields = all_fields_except_id(ToolUsage)
