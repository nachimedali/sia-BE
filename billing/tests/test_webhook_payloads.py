"""Webhook payload handling under everything Stripe actually sends.

Stripe expands objects inconsistently (`customer` is a string here, an object
there), delivers events out of order, and sends subscriptions for prices we may
not recognise. None of those may produce a partial write or a 500 — a 500 makes
Stripe retry an event that will fail identically, forever.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from billing.models import CreditLedger, StripeEvent, Subscription, SubscriptionStatus
from billing.services import ledger, webhooks
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def pro(plans):
    plan = plans["pro"]
    plan.stripe_price_id_monthly = "price_pro_monthly"
    plan.save()
    return plan


def _subscription(workspace, price_id="price_pro_monthly", **overrides):
    now = int(timezone.now().timestamp())
    obj = {
        "id": "sub_1",
        "status": "active",
        "customer": "cus_1",
        "cancel_at_period_end": False,
        "current_period_start": now,
        "current_period_end": now + 2_592_000,
        "metadata": {"workspace_id": str(workspace.pk)},
        "items": {"data": [{"price": {"id": price_id}}]},
    }
    obj.update(overrides)
    return {"id": "evt_1", "type": "customer.subscription.created", "data": {"object": obj}}


# -----------------------------------------------------------------------------
# Malformed / unattributable
# -----------------------------------------------------------------------------
def test_an_event_without_an_id_is_ignored_rather_than_stored() -> None:
    """Without an id there is nothing to deduplicate on, so accepting it would
    mean processing it again on every retry."""
    assert webhooks.process_event({"type": "customer.subscription.created"}) is False
    assert not StripeEvent.objects.exists()


def test_a_subscription_for_an_unknown_workspace_is_dropped_quietly(pro) -> None:
    ghost = {
        "id": "evt_ghost",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_x",
                "status": "active",
                "customer": "cus_nobody",
                "metadata": {},
                "items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
            }
        },
    }

    assert webhooks.process_event(ghost) is True
    assert not Subscription.objects.exists()


def test_a_subscription_is_attributed_by_customer_when_metadata_is_missing(workspace, pro) -> None:
    """Subscriptions created in the Stripe dashboard carry no metadata, so the
    customer id is the only link back."""
    workspace.stripe_customer_id = "cus_1"
    workspace.save(update_fields=["stripe_customer_id"])

    webhooks.process_event(_subscription(workspace, metadata={}))

    assert Subscription.objects.get(stripe_subscription_id="sub_1").workspace == workspace


def test_a_price_we_do_not_recognise_is_refused_rather_than_guessed(workspace, pro) -> None:
    """Assigning the wrong plan is worse than assigning none: the user would be
    silently entitled to something they did not buy."""
    webhooks.process_event(_subscription(workspace, price_id="price_from_another_product"))

    assert not Subscription.objects.exists()
    workspace.refresh_from_db()
    assert workspace.plan.code == "free"


def test_a_subscription_with_no_line_items_is_refused(workspace, pro) -> None:
    webhooks.process_event(_subscription(workspace, items={"data": []}))

    assert not Subscription.objects.exists()


def test_an_expanded_customer_object_is_read_the_same_as_a_string(workspace, pro) -> None:
    """Stripe expands `customer` depending on the API version and the request."""
    webhooks.process_event(
        {
            "id": "evt_expanded",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": str(workspace.pk),
                    "customer": {"id": "cus_expanded", "object": "customer"},
                }
            },
        }
    )

    workspace.refresh_from_db()
    assert workspace.stripe_customer_id == "cus_expanded"


def test_a_checkout_session_without_a_reference_is_ignored(workspace) -> None:
    webhooks.process_event(
        {
            "id": "evt_noref",
            "type": "checkout.session.completed",
            "data": {"object": {"customer": "cus_1"}},
        }
    )

    workspace.refresh_from_db()
    assert workspace.stripe_customer_id == ""


def test_a_checkout_session_for_a_deleted_workspace_does_not_crash(pro) -> None:
    assert Workspace.objects.filter(pk=999_999).count() == 0

    assert (
        webhooks.process_event(
            {
                "id": "evt_gone",
                "type": "checkout.session.completed",
                "data": {"object": {"client_reference_id": "999999", "customer": "cus_1"}},
            }
        )
        is True
    )


def test_an_unknown_stripe_status_does_not_entitle(workspace, pro) -> None:
    """A status Stripe adds later must not silently grant access."""
    webhooks.process_event(_subscription(workspace, status="some_future_status"))

    workspace.refresh_from_db()
    assert Subscription.objects.get(stripe_subscription_id="sub_1").status == (
        SubscriptionStatus.INCOMPLETE
    )
    assert workspace.plan.code == "free"


# -----------------------------------------------------------------------------
# Invoices
# -----------------------------------------------------------------------------
def test_an_invoice_without_a_subscription_is_ignored(workspace) -> None:
    """One-off invoices exist and carry no subscription."""
    assert (
        webhooks.process_event(
            {
                "id": "evt_oneoff",
                "type": "invoice.payment_succeeded",
                "data": {"object": {"billing_reason": "manual"}},
            }
        )
        is True
    )
    assert not CreditLedger.objects.filter(workspace=workspace).exists()


def test_an_invoice_for_an_unknown_subscription_is_ignored(workspace) -> None:
    """Reachable when the invoice lands before `customer.subscription.created`,
    which Stripe does not order."""
    webhooks.process_event(
        {
            "id": "evt_early",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"subscription": "sub_unseen", "billing_reason": "cycle"}},
        }
    )

    assert not CreditLedger.objects.filter(workspace=workspace).exists()


def test_a_payment_failure_for_an_unknown_subscription_is_ignored(workspace) -> None:
    assert (
        webhooks.process_event(
            {
                "id": "evt_fail_unknown",
                "type": "invoice.payment_failed",
                "data": {"object": {}},
            }
        )
        is True
    )


def test_a_repeated_customer_id_is_not_rewritten(workspace) -> None:
    workspace.stripe_customer_id = "cus_same"
    workspace.save(update_fields=["stripe_customer_id"])
    before = workspace.updated_at

    webhooks.process_event(
        {
            "id": "evt_same",
            "type": "checkout.session.completed",
            "data": {"object": {"client_reference_id": str(workspace.pk), "customer": "cus_same"}},
        }
    )

    workspace.refresh_from_db()
    assert workspace.updated_at == before


def test_a_subscription_update_does_not_re_grant(workspace, pro) -> None:
    """Stripe sends `updated` for a great many reasons — a card change must not
    hand out another month of credits."""
    webhooks.process_event(_subscription(workspace))
    balance = ledger.credit_balance(workspace)

    update = _subscription(workspace)
    update["id"] = "evt_2"
    update["type"] = "customer.subscription.updated"
    webhooks.process_event(update)

    assert ledger.credit_balance(workspace) == balance
