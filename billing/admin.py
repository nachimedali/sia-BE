"""Billing admin (implementation.md Phase 3.4).

Quota editing lives here (I8) — an operator retunes a plan without a deploy.
Everything else in this module is a guardrail, because the same screen that lets
you fix a quota lets you destroy the audit trail.
"""

from __future__ import annotations

import logging
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.http import HttpRequest

from billing.models import CreditLedger, Pack, Plan, StripeEvent, Subscription, VideoLedger
from billing.services import ledger

logger = logging.getLogger(__name__)


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """No add, no change, no delete. For the append-only records.

    The ledgers are the evidence in a billing dispute; a screen that can edit
    them is a screen that can lose the dispute.
    """

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "code",
        "display_name",
        "price_monthly_cents",
        "monthly_ai_credits",
        "included_videos",
        "max_social_accounts",
        "is_public",
        "sort_order",
    )
    list_filter = ("is_public",)
    search_fields = ("code", "display_name")
    save_on_top = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        # D13: the code is the join key for Stripe mapping and analytics.
        return ("code",) if obj else ()

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deleting a plan with subscribers would orphan live workspaces.

        Deprecate by unsetting `is_public` instead: existing subscribers keep
        their entitlements and the plan disappears from the pricing page.
        """
        if obj is None:
            return True
        return not obj.workspaces.exists() and not obj.subscriptions.exists()

    def save_model(self, request: HttpRequest, obj: Plan, form: Any, change: bool) -> None:
        if change and form.changed_data:
            # Quota changes are a revenue event. Logged with the actor so a
            # "why does everyone suddenly have 400 credits" question has an answer.
            logger.warning(
                "plan edited in admin",
                extra={
                    "plan": obj.code,
                    "changed": form.changed_data,
                    "actor": getattr(request.user, "email", "?"),
                },
            )
        super().save_model(request, obj, form, change)


@admin.register(Pack)
class PackAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Pack size and price are editable here for the same reason plan quotas
    are (I8). Withdraw a pack by unsetting `is_public`: deleting it would leave
    the ledger rows it produced pointing at nothing an operator can name."""

    list_display = (
        "code",
        "display_name",
        "kind",
        "units",
        "price_cents",
        "is_public",
        "sort_order",
    )
    list_filter = ("kind", "is_public")
    search_fields = ("code", "display_name")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return ("code",) if obj else ()


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyAdmin):
    """Read-only: Stripe is the source of truth (Phase 3.5). Editing here would
    produce a state the next webhook silently overwrites."""

    list_display = ("workspace", "plan", "status", "period_end", "cancel_at_period_end")
    list_filter = ("status", "plan")
    search_fields = ("workspace__name", "stripe_subscription_id")
    list_select_related = ("workspace", "plan")


class AdjustCreditsForm(forms.Form):
    delta = forms.IntegerField(
        help_text="Signed. Negative takes credits back.", label="Credits to add"
    )
    note = forms.CharField(
        max_length=200, help_text="Required — an unattributed adjustment reads as a bug."
    )


@admin.register(CreditLedger)
class CreditLedgerAdmin(ReadOnlyAdmin):
    list_display = ("workspace", "delta", "balance_after", "reason", "note", "created_at")
    list_filter = ("reason",)
    search_fields = ("workspace__name", "note")
    list_select_related = ("workspace", "actor")
    date_hierarchy = "created_at"
    actions = ("adjust_credits",)

    @admin.action(description="Adjust credits for the selected workspaces")
    def adjust_credits(self, request: HttpRequest, queryset: Any) -> Any:
        """The one supported write, and it is still append-only: it inserts a
        `MANUAL_ADJUST` row attributed to the operator rather than editing
        anything."""
        from django.shortcuts import render

        workspace_ids = sorted({entry.workspace_id for entry in queryset})
        if not workspace_ids:
            self.message_user(request, "Select at least one row.", messages.WARNING)
            return None

        if "apply" in request.POST:
            form = AdjustCreditsForm(request.POST)
            if form.is_valid():
                from workspaces.models import Workspace

                for workspace in Workspace.objects.filter(pk__in=workspace_ids):
                    ledger.adjust_credits(
                        workspace,
                        form.cleaned_data["delta"],
                        actor=request.user,
                        note=form.cleaned_data["note"],
                    )
                self.message_user(
                    request, f"Adjusted {len(workspace_ids)} workspace(s).", messages.SUCCESS
                )
                return None
        else:
            form = AdjustCreditsForm()

        return render(
            request,
            "admin/billing/adjust_credits.html",
            {"form": form, "workspace_ids": workspace_ids, "queryset": queryset},
        )


@admin.register(VideoLedger)
class VideoLedgerAdmin(ReadOnlyAdmin):
    list_display = (
        "workspace",
        "delta",
        "balance_after",
        "reason",
        "unit_cost_cents",
        "created_at",
    )
    list_filter = ("reason",)
    search_fields = ("workspace__name",)
    list_select_related = ("workspace",)
    date_hierarchy = "created_at"


@admin.register(StripeEvent)
class StripeEventAdmin(ReadOnlyAdmin):
    list_display = ("event_id", "event_type", "received_at", "processed_at", "error")
    list_filter = ("event_type",)
    search_fields = ("event_id",)
    date_hierarchy = "received_at"
