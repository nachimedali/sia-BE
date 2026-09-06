"""Billing serialisation (design.md §7).

The plan list is what the public pricing page renders (I8): the marketing copy
and the enforced quota are the same row, so retuning a plan in admin changes
both without a deploy.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from billing.models import CreditLedger, Pack, Plan, VideoLedger


class ResolvedPriceSerializer(serializers.Serializer[Any]):
    """What this caller actually pays, in the currency they are billed in.

    `display` is rendered server-side rather than in the browser: the symbol,
    its position and the number of decimals are all per-currency facts that
    live on the `Currency` row, and duplicating that formatting in TypeScript
    would put ¥37.00 on a pricing page the day someone opens Japan.

    `is_fallback` is honest rather than hidden — it says "priced in USD because
    we do not price this in your currency yet", which is a different statement
    from "this costs dollars".
    """

    amount_minor = serializers.IntegerField(read_only=True)
    currency = serializers.CharField(source="currency.code", read_only=True)
    display = serializers.CharField(read_only=True)
    is_fallback = serializers.BooleanField(read_only=True)


class PlanSerializer(serializers.ModelSerializer[Plan]):
    features = serializers.JSONField(read_only=True)
    quotas = serializers.SerializerMethodField()
    #: What *this* caller pays, resolved against their organization's billing
    #: currency. The `price_*_cents` columns beside it are the catalogue
    #: default and stay for the pricing page's anonymous case, where there is
    #: no organization to resolve against.
    price = serializers.SerializerMethodField()
    price_annual = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "code",
            "display_name",
            "tagline",
            "price_monthly_cents",
            "price_annual_cents",
            "currency",
            "price",
            "price_annual",
            "trial_days",
            "sort_order",
            "features",
            "quotas",
        )

    def _organization(self) -> Any:
        """The caller's organization, or `None` on the public pricing page.

        `None` is the ordinary case here, not an edge one: `/billing/plans/` is
        deliberately unauthenticated so the pricing page needs no session, and
        an anonymous visitor resolves to the catalogue default.
        """
        request: Any = self.context.get("request")
        user = getattr(request, "user", None)
        if request is None or user is None or not getattr(user, "is_authenticated", False):
            return None
        from common.workspaces import request_workspace

        try:
            return request_workspace(request).organization
        except Exception:
            # A pricing page must never 500. Any failure to resolve a
            # workspace — no membership, an ambiguous account, Redis down —
            # simply means "quote the catalogue default", which is the same
            # answer an anonymous visitor gets.
            return None

    @extend_schema_field(ResolvedPriceSerializer)
    def get_price(self, obj: Plan) -> dict[str, Any]:
        from billing.services import pricing

        return ResolvedPriceSerializer(
            pricing.plan_price(obj, organization=self._organization())
        ).data

    @extend_schema_field(ResolvedPriceSerializer)
    def get_price_annual(self, obj: Plan) -> dict[str, Any]:
        from billing.services import pricing

        return ResolvedPriceSerializer(
            pricing.plan_price(obj, organization=self._organization(), cycle="annual")
        ).data

    def get_quotas(self, obj: Plan) -> dict[str, int]:
        from billing.models import QUOTA_FIELDS

        return {field: getattr(obj, field) for field in sorted(QUOTA_FIELDS)}


class EntitlementsSerializer(serializers.Serializer[Any]):
    """Documents the shape of `GET /billing/entitlements/` for the schema.

    The payload is assembled by `Entitlements.as_dict()` — the resolver is the
    one place that knows how a plan becomes an entitlement, and duplicating that
    here is exactly the scattering I5 exists to prevent.
    """

    plan_code = serializers.CharField(read_only=True)
    plan_name = serializers.CharField(read_only=True)
    features = serializers.JSONField(read_only=True)
    quotas = serializers.JSONField(read_only=True)
    credits_remaining = serializers.IntegerField(read_only=True)
    video_units_remaining = serializers.IntegerField(read_only=True)
    trial_ends_at = serializers.DateTimeField(read_only=True, allow_null=True)
    is_trialing = serializers.BooleanField(read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)
    #: The second entitlement axis (P0-24, P0-59). Org add-ons alongside the
    #: org plan — the UI needs both to render `data-plan` + `data-addons`.
    addons = serializers.ListField(child=serializers.CharField(), read_only=True)
    #: The quota trial's balance (L-4, P0-20). `null` on every other plan,
    #: which is what tells the UI not to render a trial counter at all —
    #: distinct from `0`, which would mean "the trial is spent".
    trial_posts_remaining = serializers.IntegerField(read_only=True, allow_null=True)


class CreditLedgerSerializer(serializers.ModelSerializer[CreditLedger]):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = CreditLedger
        fields = ("id", "delta", "balance_after", "reason", "note", "actor_email", "created_at")
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class VideoLedgerSerializer(serializers.ModelSerializer[VideoLedger]):
    class Meta:
        model = VideoLedger
        fields = (
            "id",
            "delta",
            "balance_after",
            "reason",
            "unit_cost_cents",
            "note",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class PackSerializer(serializers.ModelSerializer[Pack]):
    """Same rule as `PlanSerializer` (I8): the size and the price the user is
    shown are the row that will be charged and credited."""

    class Meta:
        model = Pack
        fields = (
            "code",
            "display_name",
            "tagline",
            "kind",
            "units",
            "price_cents",
            "currency",
            "sort_order",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class PurchaseRequestSerializer(serializers.Serializer[Any]):
    pack_code = serializers.SlugField()


class CheckoutRequestSerializer(serializers.Serializer[Any]):
    plan_code = serializers.SlugField()
    cycle = serializers.ChoiceField(choices=("monthly", "annual"), default="monthly")


class CheckoutResponseSerializer(serializers.Serializer[Any]):
    checkout_url = serializers.URLField(read_only=True)


class PortalResponseSerializer(serializers.Serializer[Any]):
    portal_url = serializers.URLField(read_only=True)
