from django.contrib import admin

from products.models import Product


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
