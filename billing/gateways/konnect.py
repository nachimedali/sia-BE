"""Konnect (konnect.network), the TND gateway behind `PrepaidGateway` (TN-10).

Two documented calls, `POST /payments/init-payment` and `GET /payments/{ref}`,
with the `x-api-key` header. Built from the vendor's documentation and a
recorded response shape, not yet observed live — that is TN-90's job.

Konnect's fee is absorbed (owner, 2026-11-01): `addPaymentFeesToAmount` is
always false, so the customer pays exactly the invoice total. Only TND is ever
sent: Konnect would convert a USD amount at its own rate, and the invoice must
be in dinars at ours.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings

from billing.gateways.base import BillingGatewayError
from billing.gateways.prepaid import HostedPayment, PaymentState, PaymentStatus

TIMEOUT_SECONDS = 15.0


class KonnectGateway:
    key = "konnect"

    def _client(self) -> httpx.Client:
        if not settings.KONNECT_API_KEY or not settings.KONNECT_WALLET_ID:
            raise BillingGatewayError("KONNECT_API_KEY and KONNECT_WALLET_ID must be set.")
        return httpx.Client(
            base_url=settings.KONNECT_BASE_URL.rstrip("/") + "/",
            headers={"x-api-key": settings.KONNECT_API_KEY},
            timeout=TIMEOUT_SECONDS,
        )

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise BillingGatewayError(f"Konnect answered HTTP {response.status_code}.")
        try:
            body = response.json()
        except ValueError as exc:
            raise BillingGatewayError("Konnect answered with a body that is not JSON.") from exc
        if not isinstance(body, dict):
            raise BillingGatewayError("Konnect answered with an unexpected body.")
        return body

    def create_payment(
        self,
        *,
        order_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        email: str,
        webhook_url: str,
    ) -> HostedPayment:
        if currency != "TND":
            raise BillingGatewayError(f"Konnect is only asked for TND, not {currency}.")
        payload = {
            "receiverWalletId": settings.KONNECT_WALLET_ID,
            "token": "TND",
            "amount": amount_minor,  # millimes
            "type": "immediate",
            "description": description[:280],
            "acceptedPaymentMethods": ["wallet", "bank_card", "e-DINAR"],
            "lifespan": settings.PREPAID_PAYMENT_LIFESPAN_MINUTES,
            "checkoutForm": False,
            "addPaymentFeesToAmount": False,
            "email": email,
            "orderId": order_id,
            "webhook": webhook_url,
            "theme": "light",
        }
        try:
            with self._client() as client:
                body = self._json(client.post("payments/init-payment", json=payload))
        except httpx.HTTPError as exc:
            raise BillingGatewayError(f"Konnect could not be reached: {exc}") from exc
        ref, url = body.get("paymentRef"), body.get("payUrl")
        if not ref or not url:
            raise BillingGatewayError("Konnect answered without a payment reference.")
        return HostedPayment(ref=str(ref), url=str(url))

    def fetch_payment(self, ref: str) -> PaymentStatus:
        try:
            with self._client() as client:
                body = self._json(client.get(f"payments/{quote(ref, safe='')}"))
        except httpx.HTTPError as exc:
            raise BillingGatewayError(f"Konnect could not be reached: {exc}") from exc
        payment = body.get("payment")
        if not isinstance(payment, dict):
            raise BillingGatewayError("Konnect answered without a payment.")
        # Konnect reports `completed` or `pending`; an attempt that failed
        # leaves it `pending`, so anything else is read as failed, never paid.
        status = payment.get("status")
        state: PaymentState = (
            "completed" if status == "completed" else "pending" if status == "pending" else "failed"
        )
        try:
            amount = int(payment.get("amount"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise BillingGatewayError("Konnect answered without an amount.") from exc
        wallet = payment.get("receiverWallet")
        wallet_id = (wallet.get("_id") or wallet.get("id")) if isinstance(wallet, dict) else wallet
        return PaymentStatus(
            ref=ref,
            state=state,
            amount_minor=amount,
            currency=str(payment.get("token") or ""),
            order_id=str(payment.get("orderId") or ""),
            wallet_id=str(wallet_id or ""),
        )
