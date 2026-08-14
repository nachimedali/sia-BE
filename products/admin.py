from django.contrib import admin

from common.admin import ReadOnlyAdmin, all_fields_except_id
from products.models import AutopilotConfig, AutopilotDraft, AutopilotJob, Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "name",
        "workspace",
        "completeness_score",
        "is_generation_ready",
        "created_at",
    )
    list_filter = ("is_generation_ready", "hashtags_style", "emoji_style")
    search_fields = ("name", "workspace__name")
    autocomplete_fields = ("workspace", "category")
    filter_horizontal = ("reference_images",)
    readonly_fields = ("completeness_score", "is_generation_ready")


@admin.register(AutopilotConfig)
class AutopilotConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("product", "enabled", "cadence_days", "lookahead_days", "strategy", "landing")
    list_filter = ("enabled", "strategy", "landing", "auto_approve")
    search_fields = ("product__name",)
    autocomplete_fields = ("product",)


@admin.register(AutopilotJob)
class AutopilotJobAdmin(ReadOnlyAdmin):
    """Read-only: a job row is the record of what a run actually did, and
    editing one would change the cadence's own memory of when it last ran."""

    list_display = ("id", "config", "run_at", "status")
    list_filter = ("status",)
    readonly_fields = all_fields_except_id(AutopilotJob)


@admin.register(AutopilotDraft)
class AutopilotDraftAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "product", "kind", "platform", "scheduled_for", "status", "strategy")
    list_filter = ("status", "kind", "strategy")
    search_fields = ("product__name", "caption")
    readonly_fields = ("generation", "post")
