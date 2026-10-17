from __future__ import annotations

from django.contrib import admin

from social_providers.models import ProviderCall, WebhookDelivery


@admin.register(ProviderCall)
class ProviderCallAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only: a log row edited by hand is a log nobody can trust."""

    list_display = (
        "created_at",
        "provider",
        "method",
        "path",
        "status_code",
        "kind",
        "request_id",
        "latency_ms",
    )
    list_filter = ("provider", "kind", "method", "local")
    search_fields = ("request_id", "path", "error_code")

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_change_permission(self, request: object, obj: object = None) -> bool:
        return False


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only, like the call log: what arrived is evidence."""

    list_display = ("received_at", "provider", "event", "status", "delivery_id", "attempts")
    list_filter = ("provider", "event", "status")
    search_fields = ("delivery_id", "note")

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_change_permission(self, request: object, obj: object = None) -> bool:
        return False
