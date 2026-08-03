from django.contrib import admin

from billing.models import Plan


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Quota editing lives here (I8). Full guardrails land in Phase 3."""

    list_display = ("code", "display_name", "price_monthly_cents", "is_public", "sort_order")
    list_filter = ("is_public",)
    search_fields = ("code", "display_name")

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        # D13: the code is the join key for Stripe mapping and analytics.
        return ("code",) if obj else ()
