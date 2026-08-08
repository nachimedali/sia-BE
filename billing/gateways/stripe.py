"""Stripe adapter (design.md §9, D13).

Checkout and the customer portal are hosted by Stripe on purpose: card data
never touches this service, which keeps PCI scope to SAQ-A.

`client_reference_id` carries the workspace id through Checkout so the webhook
can attribute a completed session without trusting anything the browser sends
back on the success URL.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from billing.gateways.base import (
    BillingGateway,
    CheckoutSession,
    PortalSession,
    WebhookVerificationError,
)


def _stripe() -> Any:
    """Imported lazily so the package is only needed where it is used, and so
    an unset key fails at the call rather than at startup."""
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


class StripeBillingGateway:
    def create_checkout_session(
        self,
        *,
        workspace_id: int,
        customer_id: str | None,
        customer_email: str,
        price_id: str,
        trial_days: int,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        stripe = _stripe()
        subscription_data: dict[str, Any] = {"metadata": {"workspace_id": str(workspace_id)}}
        if trial_days > 0:
            subscription_data["trial_period_days"] = trial_days

        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(workspace_id),
            "subscription_data": subscription_data,
            # Stripe enforces "one trial per customer" for us; the workspace-level
            # rule is enforced in `subscriptions.start_checkout`.
            "allow_promotion_codes": True,
        }
        if customer_id:
            params["customer"] = customer_id
        else:
            params["customer_email"] = customer_email

        session = stripe.checkout.Session.create(**params)
        return CheckoutSession(id=session["id"], url=session["url"])

    def create_portal_session(self, *, customer_id: str, return_url: str) -> PortalSession:
        stripe = _stripe()
        session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
        return PortalSession(url=session["url"])

    def verify_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        stripe = _stripe()
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except Exception as exc:  # stripe raises SignatureVerificationError / ValueError
            raise WebhookVerificationError(str(exc)) from exc
        return dict(event)


def get_billing_gateway() -> BillingGateway:
    """Resolves the configured gateway. Swapping is a settings change."""
    if getattr(settings, "USE_FAKE_BILLING", False):
        from billing.gateways.fake import _fake_gateway

        return _fake_gateway
    return StripeBillingGateway()
