"""The PrepaidGateway port (TN-03, `payments-tunisia.md` §4).

A second port beside `BillingGateway`, not a second adapter for it.
`BillingGateway` is shaped around a recurring subscription — a portal,
quantity proration, signed webhooks — and Konnect sells none of those: a
Konnect adapter would have to fake three of its four methods, and a fake that
pretends a portal exists is the check that always passes (C-11). This one is
two calls: ask for a hosted one-off payment, and read a payment back.

**Reading back is the only thing that grants.** Konnect's notification is an
unsigned `GET` carrying a reference, so `fetch_payment` is called on every
notification and on every sweep, and its answer is compared against the order
row; the notification itself proves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from django.conf import settings

from billing.gateways.base import BillingGatewayError
from billing.gateways.fake import PaymentsNotConfiguredError

PaymentState = Literal["pending", "completed", "failed"]


@dataclass(frozen=True)
class HostedPayment:
    ref: str
    url: str


@dataclass(frozen=True)
class PaymentStatus:
    """What the gateway says happened, in its own terms. Compared, never trusted."""

    ref: str
    state: PaymentState
    amount_minor: int
    currency: str
    order_id: str
    #: Whose wallet received it. Blank when the gateway did not say; when it
    #: does say, it must be ours.
    wallet_id: str = ""


class PrepaidGateway(Protocol):
    key: str

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
        """A hosted page for one payment of exactly `amount_minor`.

        Raises `BillingGatewayError` when the gateway refuses or cannot be
        reached; the order then stays `PENDING` without a reference, which
        grants nothing and expires.
        """
        ...

    def fetch_payment(self, ref: str) -> PaymentStatus:
        """The payment as the gateway holds it. Raises `BillingGatewayError`."""
        ...


class FakePrepaidGateway:
    """Records calls; a payment completes when a test says so.

    `complete()` may report a different amount, currency or order id, which is
    how the mismatch path is tested: a fake that could only ever agree with us
    would leave the comparison untested.
    """

    key = "konnect"

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.payments: dict[str, PaymentStatus] = {}
        self.fail_create = False
        self.fail_fetch = False

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
        if self.fail_create:
            raise BillingGatewayError("The gateway refused the payment.")
        ref = f"fake_{order_id}_{len(self.created) + 1}"
        self.created.append(
            {
                "ref": ref,
                "order_id": order_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "description": description,
                "email": email,
                "webhook_url": webhook_url,
            }
        )
        self.payments[ref] = PaymentStatus(ref, "pending", amount_minor, currency, order_id)
        return HostedPayment(ref=ref, url=f"https://pay.test/{ref}")

    def fetch_payment(self, ref: str) -> PaymentStatus:
        if self.fail_fetch:
            raise BillingGatewayError("The gateway could not be reached.")
        if ref not in self.payments:
            raise BillingGatewayError(f"Unknown payment {ref}.")
        return self.payments[ref]

    def complete(
        self,
        ref: str,
        *,
        state: PaymentState = "completed",
        amount_minor: int | None = None,
        currency: str | None = None,
        order_id: str | None = None,
        wallet_id: str | None = None,
    ) -> None:
        current = self.payments[ref]
        self.payments[ref] = PaymentStatus(
            ref,
            state,
            current.amount_minor if amount_minor is None else amount_minor,
            currency or current.currency,
            current.order_id if order_id is None else order_id,
            wallet_id or current.wallet_id,
        )

    def clear(self) -> None:
        self.created.clear()
        self.payments.clear()
        self.fail_create = self.fail_fetch = False


class RefusingPrepaidGateway:
    """A deployment without a Konnect key: refuses, never fakes (F1)."""

    key = "konnect"

    def create_payment(self, **_: object) -> HostedPayment:
        raise PaymentsNotConfiguredError()

    def fetch_payment(self, ref: str) -> PaymentStatus:
        raise BillingGatewayError("Payments are not configured on this server.")


_fake = FakePrepaidGateway()
_refusing = RefusingPrepaidGateway()


def get_prepaid_gateway() -> PrepaidGateway:
    if getattr(settings, "USE_FAKE_PREPAID", True):
        return _refusing if getattr(settings, "FAKE_BILLING_REFUSES", False) else _fake
    from billing.gateways.konnect import KonnectGateway

    return KonnectGateway()
