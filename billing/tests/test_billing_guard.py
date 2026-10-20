"""A deployment with no Stripe key must not run the fake gateway's money paths (F1, C1).

The fake verifies webhooks against `FAKE_WEBHOOK_SECRET`, a constant in the
source. On a server that has no Stripe key, `USE_FAKE_BILLING` resolves true and
anyone who reads the repository can sign a "paid" checkout or a subscription.
So production settings set `FAKE_BILLING_REFUSES`: the fake is still selected (the
rest of billing — plans, entitlements, ledgers — keeps working), but every call
that moves money or grants a plan answers 503 "payments are not configured".
"""

from __future__ import annotations

import json

import pytest
from rest_framework.test import APIClient

from billing.gateways.base import BillingGatewayError
from billing.gateways.fake import _fake_gateway
from billing.gateways.stripe import StripeBillingGateway, get_billing_gateway
from billing.models import CreditLedger, StripeEvent

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/api/v1/billing/webhook/stripe/"


@pytest.fixture
def refusing(settings):
    settings.USE_FAKE_BILLING = True
    settings.FAKE_BILLING_REFUSES = True


def _forged(payload: dict):
    body = json.dumps(payload).encode()
    return APIClient().post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=_fake_gateway.sign(body),
    )


def test_a_webhook_signed_with_the_public_fake_secret_is_refused(refusing, workspace) -> None:
    before = CreditLedger.objects.count()

    response = _forged(
        {
            "id": "evt_forged",
            "type": "checkout.session.completed",
            "data": {"object": {"mode": "payment", "payment_status": "paid"}},
        }
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payments_not_configured"
    assert not StripeEvent.objects.exists()
    assert CreditLedger.objects.count() == before


def test_subscribing_is_refused(refusing, auth_client, workspace, plans) -> None:
    plans["pro"].stripe_price_id_monthly = "price_pro_monthly"
    plans["pro"].save()

    response = auth_client.post(
        "/api/v1/billing/subscribe/", {"plan_code": "pro", "cycle": "monthly"}, format="json"
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payments_not_configured"


def test_the_portal_is_refused(refusing, auth_client, workspace) -> None:
    workspace.organization.stripe_customer_id = "cus_9"
    workspace.organization.save(update_fields=["stripe_customer_id"])

    response = auth_client.post("/api/v1/billing/portal/", format="json")

    assert response.status_code == 503


def test_a_quantity_update_fails_like_a_refused_gateway(refusing) -> None:
    """A gateway error, not a 503: the caller parks the workspace as
    `PENDING_BILLING` (P0-18) rather than refusing the person creating it."""
    with pytest.raises(BillingGatewayError):
        get_billing_gateway().update_subscription_quantity(subscription_item_id="si_1", quantity=2)


def test_development_keeps_the_working_fake(settings) -> None:
    settings.USE_FAKE_BILLING = True
    settings.FAKE_BILLING_REFUSES = False

    assert get_billing_gateway() is _fake_gateway


def test_a_configured_stripe_key_is_never_refused(settings) -> None:
    settings.USE_FAKE_BILLING = False
    settings.FAKE_BILLING_REFUSES = True

    assert isinstance(get_billing_gateway(), StripeBillingGateway)
