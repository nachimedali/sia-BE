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

from billing.models import (
    FEATURE_KEYS,
    CreditLedger,
    Currency,
    Pack,
    PackPrice,
    Plan,
    PlanPrice,
    StripeEvent,
    Subscription,
    VideoLedger,
)
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


class ImmutableCodeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """The admin half of D13, for the `CatalogueItem` models.

    The model refuses a changed code anyway; greying the field out is what stops
    an operator discovering that by way of a validation error.
    """

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        # D13: the code is the join key for Stripe mapping and analytics.
        return ("code",) if obj else ()


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Add a currency here; no deploy needed.

    That is the whole reason this is a table rather than an enum (I8, Part 7
    rule 10) — a new market opens on a Tuesday, and waiting for a release to
    price for it is the wrong constraint.

    **`minor units` is the field to get right.** Amounts are stored in the
    currency's smallest unit everywhere, and that unit is not always 1/100:
    JPY and KRW have none, a few dinars have three. Getting it wrong corrupts
    no stored amount — arithmetic never reads it — but it will render ¥3700 as
    ¥37.00 on the pricing page.

    Withdraw a currency by unsetting *is active*: deleting one is refused by
    the database while any price or organization still points at it.
    """

    list_display = ("code", "name", "symbol", "minor_units", "symbol_first", "is_active")
    list_editable = ("symbol", "minor_units", "symbol_first", "is_active")
    list_filter = ("is_active", "minor_units")
    search_fields = ("code", "name")


class PlanPriceInline(admin.TabularInline):  # type: ignore[type-arg]
    """Per-country pricing, edited beside the plan it prices.

    An inline rather than its own screen because a price is meaningless without
    the plan — and because setting one currency's price while looking at
    another's is how two markets end up accidentally identical.

    Amounts are in the currency's **minor units**: 3700 is $37.00, and 3700 is
    also ¥3700. Leave a Stripe id blank and checkout falls back to the plan's
    own — which is what makes adding a second currency safe before its Stripe
    prices exist.
    """

    model = PlanPrice
    extra = 0
    fields = (
        "currency",
        "monthly_cents",
        "annual_cents",
        "per_workspace_cents",
        "stripe_price_id_monthly",
        "stripe_price_id_annual",
        "is_default",
    )
    autocomplete_fields = ("currency",)


class PackPriceInline(admin.TabularInline):  # type: ignore[type-arg]
    """The same, for a pack. Note there is no `units` here: a pack grants the
    same number of credits everywhere and only the price changes — varying both
    would make two markets incomparable in every revenue question."""

    model = PackPrice
    extra = 0
    fields = ("currency", "amount_cents", "stripe_price_id", "is_default")
    autocomplete_fields = ("currency",)


@admin.register(Plan)
class PlanAdmin(ImmutableCodeAdmin):
    """**This screen is where every commercial number lives** (I8, Part 7 rule
    10). Prices, quotas, caps and the per-plan feature map are rows, not
    constants and not environment variables — so raising a credit allowance or
    changing a price is an edit here, applied on the next request, with no
    deploy and no risk of two environments disagreeing about what a customer
    bought.

    Grouped into fieldsets rather than left as one flat wall of thirty inputs:
    an operator changing a price should not have to scroll past the audience
    capture cadence to find it, and the groups are also the reader's map of
    what a plan actually *is*.

    Two things the cache does not need to be told about. The entitlement
    snapshot is keyed on `plan.updated_at`, so saving here mints a new key and
    the old one is simply never asked for again — there is no eviction step to
    forget. Balances are never cached at all.
    """

    list_display = (
        "code",
        "display_name",
        "price_monthly_cents",
        "price_per_workspace_cents",
        "max_workspaces",
        "monthly_ai_credits",
        "trial_post_quota",
        "is_public",
        "sort_order",
    )
    list_filter = ("is_public", "reaction_detail")
    search_fields = ("code", "display_name")
    save_on_top = True
    inlines = (PlanPriceInline,)

    fieldsets = (
        (
            "Identity",
            {
                "fields": (
                    "code",
                    "display_name",
                    "tagline",
                    "currency",
                    "is_public",
                    "sort_order",
                ),
                "description": (
                    "`code` is the join key for Stripe mapping and analytics, so it is "
                    "frozen once the row exists (D13). Withdraw a plan by unsetting "
                    "<em>is public</em> — deleting one would orphan its subscribers."
                ),
            },
        ),
        (
            "Price",
            {
                "fields": (
                    "price_monthly_cents",
                    "price_annual_cents",
                    "price_per_workspace_cents",
                    "stripe_price_id_monthly",
                    "stripe_price_id_annual",
                ),
                "description": (
                    "The <strong>default</strong> price, in the currency's minor units. "
                    "Per-country prices are the <em>Plan prices</em> rows below, and they "
                    "take precedence for an organization billed in that currency — €37 is "
                    "a separate decision from $37, not a conversion of it. One "
                    "subscription per organization with quantity = workspace count, so "
                    "<em>price per workspace</em> is what the quantity multiplies."
                ),
            },
        ),
        (
            "Trial",
            {
                "fields": ("trial_days", "trial_post_quota"),
                "description": (
                    "Two different trials. <em>trial days</em> is a clock, for the paid "
                    "plans. <em>trial post quota</em> is the quota trial that replaced "
                    "Free-forever — N posts, no expiry, no card, pooled across the whole "
                    "organization. Set one or the other, not both."
                ),
            },
        ),
        (
            "Quotas",
            {
                "fields": (
                    "monthly_ai_credits",
                    "included_videos",
                    "max_social_accounts",
                    "max_workspaces",
                    "max_workspace_members",
                    "max_products",
                    "max_scheduled_posts",
                    "max_autopublish_posts",
                    "scheduling_horizon_days",
                    "max_labels",
                    "max_campaigns",
                    "included_views",
                    "version_history_days",
                    "storage_bytes",
                    "counts_docs_against_quota",
                ),
                "description": (
                    "<strong>-1 means unlimited</strong> — except for "
                    "<em>max social accounts</em>, which is per-seat cost with the "
                    "publishing provider and may never be unlimited (I6). The model "
                    "refuses that combination rather than letting it save."
                ),
            },
        ),
        (
            "Audience engagement",
            {
                "fields": ("comment_capture_interval_minutes", "reaction_detail"),
                "description": (
                    "Reading comments is free from the vendor, so this ladder is "
                    "freshness and depth rather than cost. "
                    "<strong>0 minutes means webhook-driven</strong>, not "
                    "&ldquo;poll constantly&rdquo;: the provider caches its read "
                    "endpoints for ten minutes and says plainly not to poll them."
                ),
            },
        ),
        (
            "Features",
            {
                "fields": ("features",),
                "description": (
                    "A JSON object. Unknown keys are rejected on save, so a typo is a "
                    "validation error rather than a feature that silently never turns "
                    "on. Valid keys: <code>"
                    + "</code>, <code>".join(sorted(FEATURE_KEYS))
                    + "</code>. Most are booleans; "
                    "<code>analytics_history_days</code> is a number of days."
                ),
            },
        ),
    )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Deleting a plan with subscribers would orphan live workspaces.

        Deprecate by unsetting `is_public` instead: existing subscribers keep
        their entitlements and the plan disappears from the pricing page.
        """
        if obj is None:
            return True
        return not obj.workspaces.exists() and not obj.subscriptions.exists()

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        """`code` is read-only once the row exists, and Django refuses to render
        a read-only field it also finds in `fieldsets` unless it is declared —
        so on an edit the identity group drops it into `readonly_fields`'
        keeping instead of listing it twice."""
        return self.fieldsets

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
class PackAdmin(ImmutableCodeAdmin):
    """Pack size and price are editable here for the same reason plan quotas
    are (I8). Withdraw a pack by unsetting `is_public`: deleting it would leave
    the ledger rows it produced pointing at nothing an operator can name.

    A pack is a prepaid top-up rather than a subscription, which is why it has
    `units` and no recurrence: buying one appends a ledger row, and the balance
    is the sum of the rows. Changing `units` here changes what the *next*
    purchase grants and never what a past one did.
    """

    list_display = (
        "code",
        "display_name",
        "kind",
        "units",
        "price_cents",
        "currency",
        "is_public",
        "sort_order",
    )
    list_filter = ("kind", "is_public")
    search_fields = ("code", "display_name")
    inlines = (PackPriceInline,)


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
