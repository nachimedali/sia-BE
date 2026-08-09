"""BillingGateway contract (design.md §9, A8).

One suite, both implementations. The fake runs everywhere; the real adapter runs
against a stub `stripe` module so its **parameter assembly** is covered without
credentials — that is where the bugs actually are (a misnamed
`trial_period_days`, a missing `client_reference_id`) and none of them would be
caught by testing the fake alone.

A run against live Stripe test-mode keys is marked `integration` and skipped
without them.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from django.test import override_settings

from billing.gateways.base import WebhookVerificationError
from billing.gateways.fake import FakeBillingGateway
from billing.gateways.stripe import StripeBillingGateway, get_billing_gateway


class _StripeStub(types.ModuleType):
    """Stands in for the `stripe` package, recording what it was called with."""

    def __init__(self) -> None:
        super().__init__("stripe")
        self.api_key: str | None = None
        self.created: dict[str, Any] = {}
        self.portal: dict[str, Any] = {}
        self.constructed: tuple[Any, ...] | None = None

        stub = self

        class _Session:
            @staticmethod
            def create(**params: Any) -> dict[str, str]:
                stub.created = params
                return {"id": "cs_live_1", "url": "https://checkout.stripe.com/cs_live_1"}

        class _PortalSession:
            @staticmethod
            def create(**params: Any) -> dict[str, str]:
                stub.portal = params
                return {"url": "https://billing.stripe.com/p/1"}

        class _Webhook:
            @staticmethod
            def construct_event(payload: bytes, signature: str, secret: str) -> dict[str, Any]:
                stub.constructed = (payload, signature, secret)
                if signature != "good":
                    raise ValueError("Signature mismatch")
                return json.loads(payload)

        self.checkout = types.SimpleNamespace(Session=_Session)
        self.billing_portal = types.SimpleNamespace(Session=_PortalSession)
        self.Webhook = _Webhook


@pytest.fixture
def stripe_stub(monkeypatch):
    stub = _StripeStub()
    monkeypatch.setitem(sys.modules, "stripe", stub)
    return stub


# -----------------------------------------------------------------------------
# The contract, run against both implementations
# -----------------------------------------------------------------------------
@pytest.fixture(params=["fake", "stripe"])
def gateway(request, stripe_stub):
    return FakeBillingGateway() if request.param == "fake" else StripeBillingGateway()


def test_checkout_returns_a_usable_url(gateway) -> None:
    session = gateway.create_checkout_session(
        mode="subscription",
        workspace_id=7,
        customer_id=None,
        customer_email="jordan@example.com",
        price_id="price_pro_monthly",
        trial_days=7,
        success_url="https://app.test/ok",
        cancel_url="https://app.test/no",
    )

    assert session.id
    assert session.url.startswith("https://")


def test_portal_returns_a_usable_url(gateway) -> None:
    assert gateway.create_portal_session(
        customer_id="cus_1", return_url="https://app.test/billing"
    ).url.startswith("https://")


# -----------------------------------------------------------------------------
# Real-adapter parameter assembly
# -----------------------------------------------------------------------------
def test_the_workspace_id_travels_through_checkout(stripe_stub) -> None:
    """`client_reference_id` is how the webhook attributes a completed session
    without trusting anything the browser sends back on the success URL."""
    StripeBillingGateway().create_checkout_session(
        mode="subscription",
        workspace_id=42,
        customer_id=None,
        customer_email="jordan@example.com",
        price_id="price_pro_monthly",
        trial_days=7,
        success_url="https://app.test/ok",
        cancel_url="https://app.test/no",
    )

    assert stripe_stub.created["client_reference_id"] == "42"
    assert stripe_stub.created["subscription_data"]["metadata"]["workspace_id"] == "42"
    assert stripe_stub.created["subscription_data"]["trial_period_days"] == 7
    assert stripe_stub.created["mode"] == "subscription"


def test_no_trial_means_no_trial_key_at_all(stripe_stub) -> None:
    """`trial_period_days=0` is not the same as omitting it — Stripe rejects it."""
    StripeBillingGateway().create_checkout_session(
        mode="subscription",
        workspace_id=42,
        customer_id=None,
        customer_email="jordan@example.com",
        price_id="price_pro_monthly",
        trial_days=0,
        success_url="s",
        cancel_url="c",
    )

    assert "trial_period_days" not in stripe_stub.created["subscription_data"]


def test_a_known_customer_is_reused_rather_than_duplicated(stripe_stub) -> None:
    """Passing `customer_email` for an existing customer creates a second one,
    and the portal then shows them the wrong subscription."""
    StripeBillingGateway().create_checkout_session(
        mode="subscription",
        workspace_id=42,
        customer_id="cus_existing",
        customer_email="jordan@example.com",
        price_id="price_pro_monthly",
        trial_days=0,
        success_url="s",
        cancel_url="c",
    )

    assert stripe_stub.created["customer"] == "cus_existing"
    assert "customer_email" not in stripe_stub.created


def test_a_payment_carries_its_metadata_on_the_session(stripe_stub) -> None:
    """A one-off payment has no subscription to hang metadata on, so the pack
    code rides on the session — which is what `checkout.session.completed`
    delivers, and the only thing fulfilment has to go on."""
    StripeBillingGateway().create_checkout_session(
        mode="payment",
        workspace_id=42,
        customer_id=None,
        customer_email="jordan@example.com",
        price_id="price_credits_500",
        metadata={"pack_code": "credits-500"},
        success_url="s",
        cancel_url="c",
    )

    assert stripe_stub.created["mode"] == "payment"
    assert stripe_stub.created["metadata"] == {
        "pack_code": "credits-500",
        "workspace_id": "42",
    }
    assert "subscription_data" not in stripe_stub.created


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
def test_the_real_adapter_verifies_against_the_configured_secret(stripe_stub) -> None:
    payload = json.dumps({"id": "evt_1", "type": "ping"}).encode()

    event = StripeBillingGateway().verify_webhook(payload, "good")

    assert event["id"] == "evt_1"
    assert stripe_stub.constructed == (payload, "good", "whsec_test")


def test_the_real_adapter_translates_a_bad_signature(stripe_stub) -> None:
    """Stripe's own exception type must not leak past the port, or the view has
    to know which SDK is behind it."""
    payload = json.dumps({"id": "evt_1"}).encode()

    with pytest.raises(WebhookVerificationError):
        StripeBillingGateway().verify_webhook(payload, "forged")


def test_the_api_key_is_set_from_settings(stripe_stub) -> None:
    with override_settings(STRIPE_SECRET_KEY="sk_test_123"):
        StripeBillingGateway().create_portal_session(customer_id="cus_1", return_url="r")

    assert stripe_stub.api_key == "sk_test_123"


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------
@override_settings(USE_FAKE_BILLING=True)
def test_the_fake_is_resolved_when_configured() -> None:
    assert isinstance(get_billing_gateway(), FakeBillingGateway)


@override_settings(USE_FAKE_BILLING=False)
def test_the_real_gateway_is_resolved_otherwise() -> None:
    assert isinstance(get_billing_gateway(), StripeBillingGateway)


@pytest.mark.integration
def test_live_stripe_test_mode_accepts_a_checkout_session() -> None:
    """Skipped without credentials. This is the one that would catch Stripe
    changing an API shape under us."""
    from django.conf import settings

    if not settings.STRIPE_SECRET_KEY:
        pytest.skip("no Stripe test-mode key configured")

    session = StripeBillingGateway().create_checkout_session(
        mode="subscription",
        workspace_id=1,
        customer_id=None,
        customer_email="integration@example.com",
        price_id=settings.STRIPE_PRICE_ID_FOR_TESTS,
        trial_days=0,
        success_url="https://example.com/ok",
        cancel_url="https://example.com/no",
    )
    assert session.url.startswith("https://")
