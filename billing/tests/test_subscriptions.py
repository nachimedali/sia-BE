"""Subscription lifecycle, webhooks and downgrade (design.md §8.1, I10)."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import time_machine
from django.utils import timezone

from billing.gateways.fake import _fake_gateway
from billing.models import (
    CreditLedger,
    CreditReason,
    StripeEvent,
    Subscription,
    SubscriptionStatus,
    VideoLedger,
)
from billing.services import ledger, subscriptions, webhooks
from common.exceptions import OCCSError, StateConflict

pytestmark = pytest.mark.django_db


@pytest.fixture
def priced_plans(plans):
    """Stripe price ids, which `seed_plans` deliberately leaves blank — they
    differ per Stripe account and are configuration, not seed data."""
    for code in ("pro", "advanced"):
        plan = plans[code]
        plan.stripe_price_id_monthly = f"price_{code}_monthly"
        plan.stripe_price_id_annual = f"price_{code}_annual"
        plan.save()
    return plans


def _subscription_event(
    workspace,
    plan,
    *,
    event_id="evt_1",
    event_type="customer.subscription.created",
    status="active",
    subscription_id="sub_1",
    cycle="monthly",
):
    price_id = plan.stripe_price_id_annual if cycle == "annual" else plan.stripe_price_id_monthly
    now = int(timezone.now().timestamp())
    return {
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": subscription_id,
                "status": status,
                "customer": "cus_1",
                "cancel_at_period_end": False,
                "current_period_start": now,
                "current_period_end": now + 30 * 24 * 3600,
                "metadata": {"workspace_id": str(workspace.pk)},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


# -----------------------------------------------------------------------------
# Checkout
# -----------------------------------------------------------------------------
def test_checkout_offers_the_trial_only_once(workspace, priced_plans) -> None:
    """§8.1 — one trial per workspace. Stripe enforces the same per billing
    identity, which is what catches a second workspace on one card."""
    subscriptions.start_checkout(
        workspace, plan_code="pro", cycle="monthly", success_url="s", cancel_url="c"
    )
    assert _fake_gateway.checkout_calls[-1]["trial_days"] == 7

    workspace.trial_ends_at = timezone.now() - dt.timedelta(days=1)
    workspace.save(update_fields=["trial_ends_at"])

    subscriptions.start_checkout(
        workspace, plan_code="pro", cycle="monthly", success_url="s", cancel_url="c"
    )
    assert _fake_gateway.checkout_calls[-1]["trial_days"] == 0


def test_checkout_uses_the_annual_price_when_asked(workspace, priced_plans) -> None:
    subscriptions.start_checkout(
        workspace, plan_code="pro", cycle="annual", success_url="s", cancel_url="c"
    )
    assert _fake_gateway.checkout_calls[-1]["price_id"] == "price_pro_annual"


def test_free_cannot_be_checked_out(workspace, priced_plans) -> None:
    with pytest.raises(OCCSError):
        subscriptions.start_checkout(
            workspace, plan_code="free", cycle="monthly", success_url="s", cancel_url="c"
        )


def test_a_plan_without_a_stripe_price_is_refused_as_configuration(workspace, plans) -> None:
    """Better a clear 400 than a Checkout session Stripe rejects."""
    with pytest.raises(OCCSError) as excinfo:
        subscriptions.start_checkout(
            workspace, plan_code="pro", cycle="monthly", success_url="s", cancel_url="c"
        )
    assert excinfo.value.code == "plan_not_purchasable"


def test_a_second_subscription_is_refused(workspace, priced_plans) -> None:
    Subscription.objects.create(
        workspace=workspace,
        plan=priced_plans["pro"],
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_existing",
    )
    with pytest.raises(StateConflict):
        subscriptions.start_checkout(
            workspace, plan_code="advanced", cycle="monthly", success_url="s", cancel_url="c"
        )


def test_portal_needs_a_billing_customer(workspace) -> None:
    with pytest.raises(StateConflict):
        subscriptions.open_portal(workspace, return_url="r")


# -----------------------------------------------------------------------------
# Webhooks
# -----------------------------------------------------------------------------
def test_stripe_webhook_idempotent_on_replay(workspace, priced_plans) -> None:
    """Stripe retries on any non-2xx and can deliver twice regardless. Without
    the event record, a replay grants a second month of credits."""
    event = _subscription_event(workspace, priced_plans["pro"])

    assert webhooks.process_event(event) is True
    granted_once = ledger.credit_balance(workspace)
    rows_once = CreditLedger.objects.filter(workspace=workspace).count()

    assert webhooks.process_event(event) is False

    assert ledger.credit_balance(workspace) == granted_once
    assert CreditLedger.objects.filter(workspace=workspace).count() == rows_once
    assert StripeEvent.objects.filter(event_id="evt_1").count() == 1


def test_subscription_created_moves_the_workspace_onto_the_plan(workspace, priced_plans) -> None:
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))

    workspace.refresh_from_db()
    assert workspace.plan.code == "pro"
    assert ledger.credit_balance(workspace) == 150
    assert ledger.video_balance(workspace) == 4

    subscription = Subscription.objects.get(stripe_subscription_id="sub_1")
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.period_end is not None


def test_a_trialing_subscription_grants_under_the_trial_reason(workspace, priced_plans) -> None:
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"], status="trialing"))

    workspace.refresh_from_db()
    assert workspace.trial_ends_at is not None
    assert CreditLedger.objects.filter(
        workspace=workspace, reason=CreditReason.TRIAL_GRANT
    ).exists()


def test_subscription_deleted_downgrades_without_destroying_anything(
    workspace, priced_plans
) -> None:
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))
    history = CreditLedger.objects.filter(workspace=workspace).count()

    webhooks.process_event(
        _subscription_event(
            workspace,
            priced_plans["pro"],
            event_id="evt_2",
            event_type="customer.subscription.deleted",
            status="canceled",
        )
    )

    workspace.refresh_from_db()
    assert workspace.plan.code == "free"
    # I10: the ledger only grew. Nothing was rewritten and nothing removed.
    assert CreditLedger.objects.filter(workspace=workspace).count() > history


def test_renewal_grants_but_the_first_invoice_does_not_double_grant(
    workspace, priced_plans
) -> None:
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))
    after_create = ledger.credit_balance(workspace)

    # The invoice that accompanies the very first subscription.
    webhooks.process_event(
        {
            "id": "evt_first_invoice",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "subscription": "sub_1",
                    "billing_reason": "subscription_create",
                }
            },
        }
    )
    assert ledger.credit_balance(workspace) == after_create

    ledger.debit_credits(workspace, 100, quota=150)
    webhooks.process_event(
        {
            "id": "evt_renewal",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"subscription": "sub_1", "billing_reason": "subscription_cycle"}},
        }
    )
    assert ledger.credit_balance(workspace) == 150


def test_payment_failure_marks_past_due_without_cutting_access(workspace, priced_plans) -> None:
    """Stripe's dunning gets several days to retry the card. Cutting access on
    the first failure loses a paying customer to a reissued card."""
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))

    webhooks.process_event(
        {
            "id": "evt_failed",
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": "sub_1"}},
        }
    )

    workspace.refresh_from_db()
    assert Subscription.objects.get(stripe_subscription_id="sub_1").status == (
        SubscriptionStatus.PAST_DUE
    )
    assert workspace.plan.code == "pro"


def test_checkout_completed_records_the_customer(workspace, priced_plans) -> None:
    webhooks.process_event(
        {
            "id": "evt_checkout",
            "type": "checkout.session.completed",
            "data": {"object": {"client_reference_id": str(workspace.pk), "customer": "cus_42"}},
        }
    )

    workspace.refresh_from_db()
    assert workspace.stripe_customer_id == "cus_42"


def test_an_unhandled_event_is_recorded_and_acknowledged(workspace) -> None:
    """Recorded so a replay is still refused, acknowledged so Stripe stops
    retrying something we will never act on."""
    assert (
        webhooks.process_event({"id": "evt_x", "type": "customer.source.updated", "data": {}})
        is True
    )
    assert StripeEvent.objects.get(event_id="evt_x").error == "unhandled event type"


def test_a_failing_handler_is_recorded_rather_than_retried_forever(
    workspace, priced_plans, monkeypatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(webhooks, "_on_subscription_change", boom)

    assert webhooks.process_event(_subscription_event(workspace, priced_plans["pro"])) is True

    record = StripeEvent.objects.get(event_id="evt_1")
    assert "handler exploded" in record.error
    assert record.processed_at is not None


def test_the_fake_gateway_verifies_signatures_for_real(workspace) -> None:
    """A fake that accepted anything would let the signature check rot, and it
    is the only thing standing between the internet and every workspace's plan."""
    from billing.gateways.base import WebhookVerificationError

    payload = json.dumps({"id": "evt_signed", "type": "ping"}).encode()

    assert _fake_gateway.verify_webhook(payload, _fake_gateway.sign(payload))["id"] == "evt_signed"
    with pytest.raises(WebhookVerificationError):
        _fake_gateway.verify_webhook(payload, "not-the-signature")


# -----------------------------------------------------------------------------
# I10 — downgrade destroys nothing
# -----------------------------------------------------------------------------
def test_downgrade_marks_over_limit_and_paused_without_deleting(workspace, priced_plans) -> None:
    """I10.

    The two marking effects the name promises belong to models that arrive
    later — `Post` in Phase 4 and `SocialAccount` in Phase 9 — and both attach
    to `downgrade_to_free`, which is why it is a service rather than a branch in
    the webhook. What is assertable now is the part that must hold regardless:
    a downgrade is a billing event, not a data event.
    """
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))
    workspace.refresh_from_db()

    spend = ledger.debit_credits(workspace, 20, quota=150)
    credit_rows = set(CreditLedger.objects.filter(workspace=workspace).values_list("pk", flat=True))
    video_rows = set(VideoLedger.objects.filter(workspace=workspace).values_list("pk", flat=True))

    subscriptions.downgrade_to_free(workspace, reason="test")

    workspace.refresh_from_db()
    assert workspace.plan.code == "free"

    surviving_credits = set(
        CreditLedger.objects.filter(workspace=workspace).values_list("pk", flat=True)
    )
    surviving_videos = set(
        VideoLedger.objects.filter(workspace=workspace).values_list("pk", flat=True)
    )
    assert credit_rows <= surviving_credits, "a downgrade must not remove ledger history"
    assert video_rows <= surviving_videos
    # The spend that already happened stays spent.
    assert CreditLedger.objects.get(pk=spend.pk).delta == -20
    # And the workspace is now on the Free allowance.
    assert ledger.credit_balance(workspace) == 30


def test_downgrading_a_free_workspace_is_a_no_op(workspace) -> None:
    before = CreditLedger.objects.filter(workspace=workspace).count()
    subscriptions.downgrade_to_free(workspace)
    assert CreditLedger.objects.filter(workspace=workspace).count() == before


def test_expire_lapsed_trials_downgrades_only_the_unconverted(
    workspace, priced_plans, user
) -> None:
    from accounts.models import User
    from workspaces.services.provisioning import provision_workspace

    workspace.plan = priced_plans["pro"]
    workspace.trial_ends_at = timezone.now() - dt.timedelta(days=1)
    workspace.save(update_fields=["plan", "trial_ends_at"])

    converted = provision_workspace(
        User.objects.create_user(email="paid@example.com", password="x"), name="Paid"
    )
    converted.plan = priced_plans["pro"]
    converted.trial_ends_at = timezone.now() - dt.timedelta(days=1)
    converted.save(update_fields=["plan", "trial_ends_at"])
    Subscription.objects.create(
        workspace=converted,
        plan=priced_plans["pro"],
        status=SubscriptionStatus.ACTIVE,
        stripe_subscription_id="sub_paid",
    )

    assert subscriptions.expire_lapsed_trials() == 1

    workspace.refresh_from_db()
    converted.refresh_from_db()
    assert workspace.plan.code == "free"
    assert converted.plan.code == "pro"


# -----------------------------------------------------------------------------
# Monthly grant
# -----------------------------------------------------------------------------
def test_grant_is_idempotent_within_a_period(workspace) -> None:
    """Derived from the ledger rather than a 'last granted' column, so running
    the sweep twice in a day cannot grant twice."""
    assert subscriptions.grant_due_period_allowances() == 1
    balance = ledger.credit_balance(workspace)

    assert subscriptions.grant_due_period_allowances() == 0
    assert ledger.credit_balance(workspace) == balance


def test_grant_fires_again_in_the_next_period(workspace) -> None:
    with time_machine.travel("2026-08-04 02:15:00+00:00", tick=False):
        subscriptions.grant_due_period_allowances()
        ledger.debit_credits(workspace, 10, quota=30)
        assert ledger.credit_balance(workspace) == 20

    with time_machine.travel("2026-09-05 02:15:00+00:00", tick=False):
        assert subscriptions.grant_due_period_allowances() == 1
        # Reset, not accumulated: credits do not roll over (§4.1).
        assert ledger.credit_balance(workspace) == 30


def test_free_period_is_anchored_on_signup_not_the_first_of_the_month(workspace) -> None:
    """Anchoring on signup spreads the grant sweep across the month instead of
    stampeding every workspace on the 1st."""
    start = subscriptions.current_period_start(workspace)
    assert start.day == workspace.created_at.day or start == workspace.created_at


def test_paid_period_follows_stripe(workspace, priced_plans) -> None:
    """So the allowance lines up with what was actually invoiced."""
    webhooks.process_event(_subscription_event(workspace, priced_plans["pro"]))
    subscription = Subscription.objects.get(stripe_subscription_id="sub_1")

    assert subscriptions.current_period_start(workspace) == subscription.period_start
