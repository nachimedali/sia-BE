"""Admin for connected accounts.

`save_model` is I6's second layer. It matters more than it looks: admin is the
one path that bypasses both the serializer and the connect flow entirely, so
without this a support engineer could hand a workspace an eleventh account on
a ten-account plan and nothing would notice until the publish-time check
refused it.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from channels import services
from channels.models import SocialAccount


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("handle", "platform", "workspace", "status", "connected_at")
    list_filter = ("platform", "status")
    search_fields = ("handle", "display_name", "provider_account_id", "workspace__name")
    autocomplete_fields = ("workspace",)
    readonly_fields = ("connected_at", "updated_at")

    def save_model(self, request: HttpRequest, obj: SocialAccount, form: Any, change: bool) -> None:
        # Only a *new* active account consumes cap. Editing an existing row —
        # fixing a handle, parking one over the limit — must not be refused by
        # the cap it is already counted against.
        if not change and obj.is_publishable:
            services.enforce_account_cap(obj.workspace)
        super().save_model(request, obj, form, change)
