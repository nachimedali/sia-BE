"""Deterministic BillingGateway for tests and dev (design.md A8)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from billing.gateways.base import CheckoutSession, PortalSession, WebhookVerificationError

FAKE_WEBHOOK_SECRET = "whsec_fake"


class FakeBillingGateway:
    """Records calls instead of making them.

    Signature verification is real HMAC rather than a no-op: a fake that accepts
    anything would let the signature check rot unnoticed, and that check is the
    only thing standing between the internet and every workspace's plan.
    """

    def __init__(self) -> None:
        self.checkout_calls: list[dict[str, Any]] = []
        self.portal_calls: list[dict[str, Any]] = []

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
        self.checkout_calls.append(
            {
                "workspace_id": workspace_id,
                "customer_id": customer_id,
                "customer_email": customer_email,
                "price_id": price_id,
                "trial_days": trial_days,
            }
        )
        session_id = f"cs_fake_{workspace_id}_{len(self.checkout_calls)}"
        return CheckoutSession(id=session_id, url=f"https://checkout.test/{session_id}")

    def create_portal_session(self, *, customer_id: str, return_url: str) -> PortalSession:
        self.portal_calls.append({"customer_id": customer_id, "return_url": return_url})
        return PortalSession(url=f"https://portal.test/{customer_id}")

    def verify_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        if not hmac.compare_digest(signature, self.sign(payload)):
            raise WebhookVerificationError("Bad signature.")
        event: dict[str, Any] = json.loads(payload)
        return event

    @staticmethod
    def sign(payload: bytes) -> str:
        return hmac.new(FAKE_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    def clear(self) -> None:
        self.checkout_calls.clear()
        self.portal_calls.clear()


# Module-level so tests can inspect calls after a view has run.
_fake_gateway = FakeBillingGateway()
