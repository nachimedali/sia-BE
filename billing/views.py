"""Billing endpoints (design.md §7).

Views parse and serialise; `billing.services` decides (implementation.md §4.1).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.http import FileResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.gateways.base import BillingGatewayError, WebhookVerificationError
from billing.gateways.stripe import get_billing_gateway
from billing.models import (
    CreditLedger,
    Currency,
    Invoice,
    Pack,
    Plan,
    PrepaidOrder,
    VideoLedger,
)
from billing.permissions import HasFlag
from billing.serializers import (
    BuyerSerializer,
    CheckoutRequestSerializer,
    CheckoutResponseSerializer,
    CreditLedgerSerializer,
    EntitlementsSerializer,
    InvoiceRowSerializer,
    PackSerializer,
    PlanSerializer,
    PortalResponseSerializer,
    PrepaidOrderSerializer,
    PrepaidSummarySerializer,
    PurchaseRequestSerializer,
    VideoLedgerSerializer,
)
from billing.services import market, prepaid, purchases, subscriptions, webhooks
from billing.services.entitlements import entitlements_for
from billing.services.flags import TN_PAYMENTS_V1
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import request_workspace

logger = logging.getLogger(__name__)


def _billing_page() -> str:
    return f"{settings.SITE_URL}/app/billing"


def _return_urls(outcome: str) -> tuple[str, str]:
    """The success and cancel URLs for a hosted Stripe page, tagged with the
    flow the user is coming back from. One place, so the billing screen can move
    without a grep across three views."""
    page = _billing_page()
    return f"{page}?{outcome}=success", f"{page}?{outcome}=cancelled"


class PlanListView(ListAPIView):  # type: ignore[type-arg]
    """Public: the pricing page renders straight from these rows (I8).

    Signed in, the caller's own plan is included even when it is no longer
    public (P4): the billing page and onboarding mark "your plan", and a
    workspace on a retired plan must still see it. Anonymous callers get only
    the public rows. Authentication is optional here, never required.
    """

    serializer_class = PlanSerializer
    pagination_class = None
    permission_classes: list[Any] = [AllowAny]

    def get_queryset(self) -> Any:
        public = Plan.objects.filter(is_public=True)
        user = getattr(self.request, "user", None)
        if user is None or not user.is_authenticated:
            return public
        from workspaces.models import Organization

        own = Organization.objects.filter(owner=user).values_list("plan_id", flat=True)
        member = Organization.objects.filter(memberships__user=user).values_list(
            "plan_id", flat=True
        )
        return Plan.objects.filter(Q(is_public=True) | Q(pk__in=own) | Q(pk__in=member)).distinct()

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
        return Response(entitlements_for(request_workspace(request)).as_dict())


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

        success_url, cancel_url = _return_urls("checkout")
        session = subscriptions.start_checkout(
            request_workspace(request),
            plan_code=payload.validated_data["plan_code"],
            cycle=payload.validated_data["cycle"],
            success_url=success_url,
            cancel_url=cancel_url,
            country=market.request_country(request),
            user=request.user,
            buyer_name=payload.validated_data["buyer_name"],
            buyer_tax_id=payload.validated_data["buyer_tax_id"],
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

        success_url, cancel_url = _return_urls("purchase")
        session = purchases.start_purchase(
            request_workspace(request),
            pack_code=payload.validated_data["pack_code"],
            success_url=success_url,
            cancel_url=cancel_url,
            country=market.request_country(request),
            user=request.user,
            buyer_name=payload.validated_data["buyer_name"],
            buyer_tax_id=payload.validated_data["buyer_tax_id"],
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
        session = subscriptions.open_portal(request_workspace(request), return_url=_billing_page())
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


# -----------------------------------------------------------------------------
# TND prepaid (Konnect) and invoices — `payments-tunisia.md`, flag tn_payments_v1
# -----------------------------------------------------------------------------
_TN: list[Any] = [IsAuthenticated, HasFlag(TN_PAYMENTS_V1)]


class KonnectWebhookView(APIView):
    """Konnect's notification: an unsigned `GET ?payment_ref=…`.

    Proves nothing by itself — `prepaid.settle` reads the payment back and
    grants only an order we created, paid in full. Konnect also sends the
    payer's browser here after paying, so it always answers with a redirect to
    the page that shows the order's state; a gateway that cannot be read now is
    read again by the sweep.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(request=None, responses={302: None}, summary="Konnect payment notification")
    def get(self, request: Request) -> HttpResponseRedirect:
        ref = str(request.query_params.get("payment_ref", ""))[:64]
        order = None
        if ref:
            try:
                order = prepaid.settle(ref)
            except BillingGatewayError:
                logger.warning("Konnect notification could not be verified yet", exc_info=True)
                order = PrepaidOrder.objects.filter(gateway_ref=ref).first()
        target = f"{_billing_page()}/return"
        return HttpResponseRedirect(f"{target}?order={order.pk}" if order else target)


class PrepaidOrderView(APIView):
    permission_classes = _TN

    @extend_schema(responses={200: PrepaidOrderSerializer}, summary="A TND order's state")
    def get(self, request: Request, pk: int) -> Response:
        workspace = request_workspace(request)
        order = (
            PrepaidOrder.objects.filter(pk=pk, organization=workspace.organization)
            .select_related("currency")
            .first()
        )
        if order is None:
            raise NotFound
        return Response(PrepaidOrderSerializer(order).data)


class PrepaidSummaryView(APIView):
    permission_classes = _TN

    @extend_schema(responses={200: PrepaidSummarySerializer}, summary="The TND prepaid period")
    def get(self, request: Request) -> Response:
        organization = request_workspace(request).organization
        if not market.uses_prepaid(organization):
            raise NotFound
        return Response(PrepaidSummarySerializer(prepaid.summary(organization)).data)


class TopUpView(APIView):
    permission_classes = _TN

    @extend_schema(
        request=BuyerSerializer,
        responses={200: CheckoutResponseSerializer},
        summary="Pay for extra workspaces until the period ends",
    )
    def post(self, request: Request) -> Response:
        payload = BuyerSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = request_workspace(request)
        if not market.uses_prepaid(workspace.organization):
            raise NotFound
        session = prepaid.start_topup(workspace, user=request.user, **payload.validated_data)
        return Response({"checkout_url": session.url})


class InvoiceListView(APIView):
    """Ours for a TND organization; Stripe's own for everyone else."""

    permission_classes = _TN

    @extend_schema(responses={200: InvoiceRowSerializer(many=True)}, summary="Invoices")
    def get(self, request: Request) -> Response:
        organization = request_workspace(request).organization
        rows: list[dict[str, Any]] = []
        if market.uses_prepaid(organization):
            for invoice in Invoice.objects.filter(organization=organization).select_related(
                "currency"
            ):
                rows.append(
                    {
                        "id": str(invoice.pk),
                        "origin": "composevision",
                        "number": invoice.number,
                        "date": invoice.issued_at.date(),
                        "period": invoice.period,
                        "total_minor": invoice.total_minor,
                        "currency": invoice.currency.code,
                        "total": invoice.currency.format(invoice.total_minor),
                        "is_credit_note": invoice.corrects_id is not None,
                        "pdf_url": "",
                    }
                )
        elif organization.stripe_customer_id:
            try:
                found = get_billing_gateway().list_invoices(
                    customer_id=organization.stripe_customer_id
                )
            except BillingGatewayError:
                logger.warning("Stripe invoices could not be listed", exc_info=True)
                found = []
            for row in found:
                currency = Currency.objects.filter(code=row.currency).first()
                rows.append(
                    {
                        "id": row.id,
                        "origin": "stripe",
                        "number": row.number,
                        "date": dt.datetime.fromtimestamp(row.created, tz=dt.UTC).date(),
                        "period": None,
                        "total_minor": row.amount_minor,
                        "currency": row.currency,
                        "total": currency.format(row.amount_minor)
                        if currency
                        else f"{row.amount_minor} {row.currency}",
                        "is_credit_note": False,
                        "pdf_url": row.pdf_url,
                    }
                )
        return Response(InvoiceRowSerializer(rows, many=True).data)


class InvoiceDocumentView(APIView):
    permission_classes = _TN

    @extend_schema(responses={(200, "application/pdf"): bytes}, summary="An invoice's PDF")
    def get(self, request: Request, pk: int) -> FileResponse:
        organization = request_workspace(request).organization
        invoice = Invoice.objects.filter(pk=pk, organization=organization).first()
        if invoice is None or not invoice.document:
            raise NotFound
        name = str(invoice.document.name).rsplit("/", 1)[-1]
        return FileResponse(invoice.document.open("rb"), as_attachment=True, filename=name)
