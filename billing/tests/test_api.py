"""Billing endpoints (design.md §7, §7.1)."""

from __future__ import annotations

import json

import pytest
from rest_framework.test import APIClient

from billing.gateways.fake import _fake_gateway
from billing.models import CreditLedger, StripeEvent, Subscription, SubscriptionStatus
from billing.services import ledger

pytestmark = pytest.mark.django_db

PLANS_URL = "/api/v1/billing/plans/"
ENTITLEMENTS_URL = "/api/v1/billing/entitlements/"
LEDGER_URL = "/api/v1/billing/ledger/"
VIDEO_LEDGER_URL = "/api/v1/billing/video-ledger/"
SUBSCRIBE_URL = "/api/v1/billing/subscribe/"
PORTAL_URL = "/api/v1/billing/portal/"
WEBHOOK_URL = "/api/v1/billing/webhook/stripe/"


@pytest.fixture
def workspace(plans, user):
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


@pytest.fixture(autouse=True)
def _reset_gateway():
    _fake_gateway.clear()
    yield
    _fake_gateway.clear()


# -----------------------------------------------------------------------------
# Plans — public, and the source the pricing page renders from (I8)
# -----------------------------------------------------------------------------
def test_plans_are_public_so_the_pricing_page_needs_no_session(plans) -> None:
    response = APIClient().get(PLANS_URL)

    assert response.status_code == 200
    assert [row["code"] for row in response.json()] == ["free", "pro", "advanced"]


def test_plan_payload_carries_the_quotas_the_pricing_page_renders(plans) -> None:
    """The marketing figure and the enforced quota are the same row, so an
    admin edit changes both without a deploy."""
    body = {row["code"]: row for row in APIClient().get(PLANS_URL).json()}

    assert body["pro"]["price_monthly_cents"] == plans["pro"].price_monthly_cents
    assert body["pro"]["quotas"]["monthly_ai_credits"] == plans["pro"].monthly_ai_credits
    assert body["pro"]["features"]["auto_publish"] is True


def test_non_public_plans_are_hidden(plans) -> None:
    plan = plans["pro"]
    plan.is_public = False
    plan.save()

    assert [row["code"] for row in APIClient().get(PLANS_URL).json()] == ["free", "advanced"]


# -----------------------------------------------------------------------------
# Entitlements
# -----------------------------------------------------------------------------
def test_entitlements_require_a_session(workspace) -> None:
    assert APIClient().get(ENTITLEMENTS_URL).status_code == 401


def test_entitlements_reflect_a_quota_edit_on_the_next_request(
    auth_client, workspace, plans
) -> None:
    """I5, end to end through HTTP — the layer the UI actually reads."""
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])

    assert auth_client.get(ENTITLEMENTS_URL).json()["quotas"]["max_products"] == 10

    plan = plans["pro"]
    plan.max_products = 42
    plan.save()

    assert auth_client.get(ENTITLEMENTS_URL).json()["quotas"]["max_products"] == 42


def test_entitlements_report_the_live_credit_balance(auth_client, workspace) -> None:
    ledger.grant_credits(workspace, 30)
    ledger.debit_credits(workspace, 4, quota=30)

    assert auth_client.get(ENTITLEMENTS_URL).json()["credits_remaining"] == 26


# -----------------------------------------------------------------------------
# Ledgers
# -----------------------------------------------------------------------------
def test_ledger_is_scoped_to_the_callers_workspace(auth_client, workspace, plans) -> None:
    """Tenancy: another workspace's spend must not be visible."""
    from accounts.models import User
    from workspaces.services.provisioning import provision_workspace

    other = provision_workspace(
        User.objects.create_user(email="other@example.com", password="x"), name="Other"
    )
    ledger.grant_credits(workspace, 30, note="mine")
    ledger.grant_credits(other, 30, note="theirs")

    rows = auth_client.get(LEDGER_URL).json()["results"]

    assert {row["note"] for row in rows} == {"mine"}


def test_ledger_is_paginated(auth_client, workspace) -> None:
    for _ in range(30):
        ledger.adjust_credits(workspace, 1, actor=None, note="tick")

    body = auth_client.get(LEDGER_URL).json()

    assert body["count"] == 30
    assert len(body["results"]) == 25


def test_video_ledger_reads(auth_client, workspace) -> None:
    ledger.grant_video_units(workspace, 4, note="allowance")

    rows = auth_client.get(VIDEO_LEDGER_URL).json()["results"]

    assert rows[0]["delta"] == 4
    assert rows[0]["reason"] == "MONTHLY_GRANT"


# -----------------------------------------------------------------------------
# Subscribe / portal
# -----------------------------------------------------------------------------
def test_subscribe_returns_a_checkout_url(auth_client, workspace, plans) -> None:
    plan = plans["pro"]
    plan.stripe_price_id_monthly = "price_pro_monthly"
    plan.save()

    response = auth_client.post(
        SUBSCRIBE_URL, {"plan_code": "pro", "cycle": "monthly"}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["checkout_url"].startswith("https://checkout.test/")
    assert _fake_gateway.checkout_calls[-1]["workspace_id"] == workspace.pk


def test_subscribing_to_free_is_a_400_in_the_envelope(auth_client, workspace) -> None:
    response = auth_client.post(SUBSCRIBE_URL, {"plan_code": "free"}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_plan"


def test_an_unknown_cycle_is_rejected(auth_client, workspace) -> None:
    response = auth_client.post(
        SUBSCRIBE_URL, {"plan_code": "pro", "cycle": "weekly"}, format="json"
    )

    assert response.status_code == 400
    assert "cycle" in response.json()["error"]["detail"]["fields"]


def test_portal_without_a_customer_is_a_409(auth_client, workspace) -> None:
    response = auth_client.post(PORTAL_URL, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_billing_customer"


def test_portal_returns_a_url_once_stripe_knows_the_customer(auth_client, workspace) -> None:
    workspace.stripe_customer_id = "cus_9"
    workspace.save(update_fields=["stripe_customer_id"])

    response = auth_client.post(PORTAL_URL, format="json")

    assert response.status_code == 200
    assert response.json()["portal_url"] == "https://portal.test/cus_9"


# -----------------------------------------------------------------------------
# Webhook
# -----------------------------------------------------------------------------
def _post_webhook(payload: dict, *, signature: str | None = None):
    body = json.dumps(payload).encode()
    return APIClient().post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=signature if signature is not None else _fake_gateway.sign(body),
    )


def test_webhook_needs_no_session_but_does_need_a_signature(workspace) -> None:
    response = _post_webhook({"id": "evt_a", "type": "ping"}, signature="forged")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert not StripeEvent.objects.exists()


def test_a_signed_webhook_is_accepted_and_recorded(workspace) -> None:
    response = _post_webhook({"id": "evt_b", "type": "ping", "data": {"object": {}}})

    assert response.status_code == 200
    assert response.json() == {"received": True, "processed": True}
    assert StripeEvent.objects.filter(event_id="evt_b").exists()


def test_a_replayed_webhook_is_acknowledged_but_not_reprocessed(workspace, plans) -> None:
    plan = plans["pro"]
    plan.stripe_price_id_monthly = "price_pro_monthly"
    plan.save()

    from django.utils import timezone

    now = int(timezone.now().timestamp())
    event = {
        "id": "evt_dup",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_dup",
                "status": "active",
                "customer": "cus_1",
                "cancel_at_period_end": False,
                "current_period_start": now,
                "current_period_end": now + 2_592_000,
                "metadata": {"workspace_id": str(workspace.pk)},
                "items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
            }
        },
    }

    assert _post_webhook(event).json()["processed"] is True
    rows = CreditLedger.objects.filter(workspace=workspace).count()

    assert _post_webhook(event).json()["processed"] is False

    assert CreditLedger.objects.filter(workspace=workspace).count() == rows
    assert Subscription.objects.filter(stripe_subscription_id="sub_dup").count() == 1
    assert Subscription.objects.get(stripe_subscription_id="sub_dup").status == (
        SubscriptionStatus.ACTIVE
    )
