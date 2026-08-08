"""The BillingGateway port (design.md §9).

Like every external dependency, Stripe sits behind a port with a real adapter
and a deterministic fake. Tests use the fake by default (A8), so the suite never
depends on Stripe being reachable or on test-mode keys being present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class WebhookVerificationError(Exception):
    """The payload did not carry a signature this gateway trusts."""


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


@dataclass(frozen=True)
class PortalSession:
    url: str


class BillingGateway(Protocol):
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
    ) -> CheckoutSession: ...

    def create_portal_session(self, *, customer_id: str, return_url: str) -> PortalSession: ...

    def verify_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        """Returns the parsed event, or raises `WebhookVerificationError`.

        Verification is not optional: the webhook is the source of truth for
        subscription state, so an unverified one is an unauthenticated write to
        every workspace's plan.
        """
        ...
