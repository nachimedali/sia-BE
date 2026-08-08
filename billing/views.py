"""Billing endpoints (design.md §7).

Views parse and serialise; `billing.services` decides (implementation.md §4.1).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.gateways.base import WebhookVerificationError
from billing.gateways.stripe import get_billing_gateway
from billing.models import CreditLedger, Pack, Plan, VideoLedger
from billing.serializers import (
    CheckoutRequestSerializer,
    CheckoutResponseSerializer,
    CreditLedgerSerializer,
    EntitlementsSerializer,
    PackSerializer,
    PlanSerializer,
    PortalResponseSerializer,
    PurchaseRequestSerializer,
    VideoLedgerSerializer,
)
from billing.services import purchases, subscriptions, webhooks
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace


class PlanListView(ListAPIView):  # type: ignore[type-arg]
    """Public: the pricing page renders straight from these rows (I8)."""

    serializer_class = PlanSerializer
    queryset = Plan.objects.filter(is_public=True)
    pagination_class = None
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(summary="List public plans", responses={200: PlanSerializer(many=True)})
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class EntitlementsView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: EntitlementsSerializer},
        summary="Resolve the caller's entitlements",
        description=(
            "What this workspace may do, right now. The UI gates on this before "
            "acting (design.md §10.5) — a 402 reaching the user as an error toast "
            "means the gate was not rendered."
        ),
    )
    def get(self, request: Request) -> Response:
        return Response(entitlements_for(active_workspace(request)).as_dict())


class _WorkspaceLedgerView(WorkspaceScopedQuerySetMixin, ListAPIView):  # type: ignore[type-arg]
    """Scoped through the shared mixin rather than by filtering here, so these
    stay inside whatever the Phase 4 tenancy sweep grows to cover."""

    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination


class CreditLedgerView(_WorkspaceLedgerView):
    queryset = CreditLedger.objects.select_related("actor")
    serializer_class = CreditLedgerSerializer

    @extend_schema(summary="Credit ledger", responses={200: CreditLedgerSerializer(many=True)})
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class VideoLedgerView(_WorkspaceLedgerView):
    queryset = VideoLedger.objects.all()
    serializer_class = VideoLedgerSerializer

    @extend_schema(summary="Video ledger", responses={200: VideoLedgerSerializer(many=True)})
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class SubscribeView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        request=CheckoutRequestSerializer,
        responses={200: CheckoutResponseSerializer},
        summary="Start a subscription",
        description=(
            "Returns a hosted Stripe Checkout URL. The subscription only becomes "
            "real when the webhook says so — an abandoned checkout entitles nobody."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = CheckoutRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        session = subscriptions.start_checkout(
            active_workspace(request),
            plan_code=payload.validated_data["plan_code"],
            cycle=payload.validated_data["cycle"],
            success_url=f"{settings.SITE_URL}/app/billing?checkout=success",
            cancel_url=f"{settings.SITE_URL}/app/billing?checkout=cancelled",
        )
        return Response({"checkout_url": session.url})


class PackListView(ListAPIView):  # type: ignore[type-arg]
    """The prepaid packs on offer (§4.3).

    Authenticated, unlike the plans: packs are a top-up for someone already
    using the product, and they are never part of the public pricing pitch.
    """

    serializer_class = PackSerializer
    queryset = Pack.objects.filter(is_public=True)
    pagination_class = None
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(summary="List purchasable packs", responses={200: PackSerializer(many=True)})
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class PurchaseView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        request=PurchaseRequestSerializer,
        responses={200: CheckoutResponseSerializer},
        summary="Buy a credit or video pack",
        description=(
            "Returns a hosted Stripe Checkout URL for a one-off payment. The "
            "units are credited by the webhook once the payment clears, never "
            "on the redirect back."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = PurchaseRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        session = purchases.start_purchase(
            active_workspace(request),
            pack_code=payload.validated_data["pack_code"],
            success_url=f"{settings.SITE_URL}/app/billing?purchase=success",
            cancel_url=f"{settings.SITE_URL}/app/billing?purchase=cancelled",
        )
        return Response({"checkout_url": session.url})


class BillingPortalView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={200: PortalResponseSerializer},
        summary="Open the Stripe customer portal",
    )
    def post(self, request: Request) -> Response:
        session = subscriptions.open_portal(
            active_workspace(request), return_url=f"{settings.SITE_URL}/app/billing"
        )
        return Response({"portal_url": session.url})


class StripeWebhookView(APIView):
    """Unauthenticated by necessity, authenticated by signature.

    CSRF is exempt because Stripe cannot carry a token; the HMAC over the raw
    body is what makes the request trustworthy, and it is checked before the
    payload is parsed.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @csrf_exempt
    @extend_schema(request=None, responses={200: None}, summary="Stripe webhook")
    def post(self, request: Request) -> Response:
        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            event = get_billing_gateway().verify_webhook(request.body, signature)
        except WebhookVerificationError as exc:
            # 400, not 403: Stripe treats any non-2xx as "retry", and a bad
            # signature is a payload problem it should stop resending.
            raise OCCSError(
                "Webhook signature verification failed.", code="invalid_signature"
            ) from exc

        processed = webhooks.process_event(event)
        return Response({"received": True, "processed": processed}, status=status.HTTP_200_OK)
