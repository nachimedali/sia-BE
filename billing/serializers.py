"""Billing serialisation (design.md §7).

The plan list is what the public pricing page renders (I8): the marketing copy
and the enforced quota are the same row, so retuning a plan in admin changes
both without a deploy.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from billing.models import CreditLedger, Pack, Plan, VideoLedger


class PlanSerializer(serializers.ModelSerializer[Plan]):
    features = serializers.JSONField(read_only=True)
    quotas = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "code",
            "display_name",
            "tagline",
            "price_monthly_cents",
            "price_annual_cents",
            "currency",
            "trial_days",
            "sort_order",
            "features",
            "quotas",
        )

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
