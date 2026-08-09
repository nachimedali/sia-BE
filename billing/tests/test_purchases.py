"""Prepaid packs — `POST /billing/purchase/` (design.md §4.3, §7, D16).

The load-bearing property is that **payment, not navigation, credits the
balance**. Everything a browser can do — reach the success URL, replay it, hit
the endpoint twice — has to leave the ledger where it was.
"""

from __future__ import annotations

import json

import pytest

from billing.gateways.fake import _fake_gateway
from billing.models import CreditLedger, CreditReason, Pack, PackKind, VideoLedger, VideoReason
from billing.services import ledger, purchases, webhooks
from common.exceptions import FeatureNotAvailable, OCCSError

pytestmark = pytest.mark.django_db

PURCHASE_URL = "/api/v1/billing/purchase/"
PACKS_URL = "/api/v1/billing/packs/"


@pytest.fixture
def packs(db):
    from django.core.management import call_command

    call_command("seed_packs", verbosity=0)
    # Stripe price ids are configuration, not seed data — `seed_packs` leaves
    # them blank for the same reason `seed_plans` does.
    for pack in Pack.objects.all():
        pack.stripe_price_id = f"price_{pack.code}"
        pack.save()
    return {pack.code: pack for pack in Pack.objects.all()}


@pytest.fixture
def pro_workspace(workspace, plans):
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    return workspace


def _paid_session(workspace, pack_code, *, event_id="evt_pack", payment_status="paid"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "mode": "payment",
                "payment_status": payment_status,
                "client_reference_id": str(workspace.pk),
                "customer": "cus_1",
                "metadata": {"pack_code": pack_code, "workspace_id": str(workspace.pk)},
            }
        },
    }


# -----------------------------------------------------------------------------
# The pack row
# -----------------------------------------------------------------------------
def test_seed_packs_is_idempotent(db) -> None:
    from django.core.management import call_command

    call_command("seed_packs", verbosity=0)
    call_command("seed_packs", verbosity=0)
    assert Pack.objects.count() == 2


def test_pack_code_is_immutable_after_creation(packs) -> None:
    """Same rule as `Plan.code` (D13): it is what a fulfilment names."""
    from django.core.exceptions import ValidationError

    pack = packs["credits-500"]
    pack.code = "credits-501"
    with pytest.raises(ValidationError):
        pack.save()


def test_a_pack_must_grant_something(db) -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        Pack.objects.create(
            code="empty", display_name="Empty", kind=PackKind.CREDITS, units=0, price_cents=100
        )


def test_a_pack_cannot_be_priced_negatively(db) -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        Pack.objects.create(
            code="paid-to-buy",
            display_name="Refund machine",
            kind=PackKind.CREDITS,
            units=10,
            price_cents=-1,
        )


# -----------------------------------------------------------------------------
# Starting a purchase
# -----------------------------------------------------------------------------
def test_starting_a_purchase_credits_nothing(workspace, packs) -> None:
    """The whole point of splitting start from fulfil: a checkout session is an
    intention, and an abandoned one must leave the balance alone."""
    before = ledger.credit_balance(workspace)

    session = purchases.start_purchase(
        workspace, pack_code="credits-500", success_url="s", cancel_url="c"
    )

    assert session.url
    assert ledger.credit_balance(workspace) == before
    assert _fake_gateway.checkout_calls[-1]["price_id"] == "price_credits-500"
    assert _fake_gateway.checkout_calls[-1]["metadata"] == {"pack_code": "credits-500"}


def test_an_unknown_or_withdrawn_pack_is_refused(workspace, packs) -> None:
    with pytest.raises(OCCSError) as excinfo:
        purchases.start_purchase(workspace, pack_code="nope", success_url="s", cancel_url="c")
    assert excinfo.value.code == "invalid_pack"

    withdrawn = packs["credits-500"]
    withdrawn.is_public = False
    withdrawn.save()

    with pytest.raises(OCCSError):
        purchases.start_purchase(
            workspace, pack_code="credits-500", success_url="s", cancel_url="c"
        )


def test_a_pack_without_a_stripe_price_is_refused_as_configuration(workspace, packs) -> None:
    pack = packs["credits-500"]
    pack.stripe_price_id = ""
    pack.save()

    with pytest.raises(OCCSError) as excinfo:
        purchases.start_purchase(
            workspace, pack_code="credits-500", success_url="s", cancel_url="c"
        )
    assert excinfo.value.code == "pack_not_purchasable"


def test_free_cannot_buy_video_because_it_has_no_way_to_spend_it(workspace, packs) -> None:
    """§4.3 — Free is 0 videos with no overage path. Selling it a video pack
    would take money for something the plan cannot use."""
    with pytest.raises(FeatureNotAvailable) as excinfo:
        purchases.start_purchase(workspace, pack_code="videos-4", success_url="s", cancel_url="c")

    assert excinfo.value.status_code == 402
    assert excinfo.value.upgrade["suggested_plan"] == "pro"


def test_a_paid_plan_can_buy_video(pro_workspace, packs) -> None:
    purchases.start_purchase(pro_workspace, pack_code="videos-4", success_url="s", cancel_url="c")
    assert _fake_gateway.checkout_calls[-1]["price_id"] == "price_videos-4"


# -----------------------------------------------------------------------------
# Fulfilment
# -----------------------------------------------------------------------------
def test_a_paid_credit_pack_lands_on_the_ledger(workspace, packs) -> None:
    before = ledger.credit_balance(workspace)

    assert webhooks.process_event(_paid_session(workspace, "credits-500")) is True

    assert ledger.credit_balance(workspace) == before + 500
    entry = CreditLedger.objects.filter(reason=CreditReason.PURCHASE).get()
    assert entry.delta == 500
    assert entry.balance_after == before + 500


def test_a_paid_video_pack_records_what_a_unit_cost(pro_workspace, packs) -> None:
    webhooks.process_event(_paid_session(pro_workspace, "videos-4"))

    entry = VideoLedger.objects.filter(reason=VideoReason.PURCHASE).get()
    assert entry.delta == 4
    # 1280c / 4 — cost + $2 at the default 8s duration (D16).
    assert entry.unit_cost_cents == 320


def test_a_replayed_delivery_does_not_credit_twice(workspace, packs) -> None:
    """Exactly-once is the `StripeEvent` row (A37). Stripe retries on any
    non-2xx and can deliver the same event more than once regardless."""
    event = _paid_session(workspace, "credits-500")

    assert webhooks.process_event(event) is True
    once = ledger.credit_balance(workspace)

    assert webhooks.process_event(event) is False
    assert ledger.credit_balance(workspace) == once
    assert CreditLedger.objects.filter(reason=CreditReason.PURCHASE).count() == 1


def test_an_unpaid_session_credits_nothing(workspace, packs) -> None:
    """A delayed payment method completes the session before the money clears."""
    webhooks.process_event(_paid_session(workspace, "credits-500", payment_status="unpaid"))

    assert not CreditLedger.objects.filter(reason=CreditReason.PURCHASE).exists()


def test_a_subscription_checkout_is_not_treated_as_a_pack(workspace, packs) -> None:
    webhooks.process_event(
        {
            "id": "evt_sub_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "mode": "subscription",
                    "client_reference_id": str(workspace.pk),
                    "customer": "cus_9",
                }
            },
        }
    )

    workspace.refresh_from_db()
    assert workspace.stripe_customer_id == "cus_9"
    assert not CreditLedger.objects.filter(reason=CreditReason.PURCHASE).exists()


def test_a_payment_for_an_unknown_pack_is_recorded_as_a_failure(workspace, packs) -> None:
    """Someone has paid for something we cannot name. Loud, and kept: the event
    row carries the payload so it can be replayed once the pack is restored."""
    from billing.models import StripeEvent

    webhooks.process_event(_paid_session(workspace, "deleted-pack"))

    record = StripeEvent.objects.get(event_id="evt_pack")
    assert "deleted-pack" in record.error
    assert record.payload["data"]["object"]["metadata"]["pack_code"] == "deleted-pack"
    assert not CreditLedger.objects.filter(reason=CreditReason.PURCHASE).exists()


def test_fulfilment_ignores_the_entitlement_gate(workspace, packs) -> None:
    """A workspace that downgraded between checkout and delivery has still
    paid. Refusing here would take the money and hand back nothing."""
    webhooks.process_event(_paid_session(workspace, "videos-4"))

    assert ledger.video_balance(workspace) == 4


def test_bought_video_units_survive_the_monthly_reset(pro_workspace, packs) -> None:
    """The difference between an allowance and a pack (§4.3): the reset takes
    back only what the monthly grant put there."""
    webhooks.process_event(_paid_session(pro_workspace, "videos-4"))
    ledger.grant_video_units(pro_workspace, 4, note="new period")

    assert ledger.video_balance(pro_workspace) == 8


def test_a_purchase_cannot_be_empty(workspace) -> None:
    with pytest.raises(ValueError):
        ledger.purchase_credits(workspace, 0, note="nothing")
    with pytest.raises(ValueError):
        ledger.purchase_video_units(workspace, 0, note="nothing")


# -----------------------------------------------------------------------------
# The endpoints
# -----------------------------------------------------------------------------
def test_purchase_requires_a_session(client, packs) -> None:
    response = client.post(
        PURCHASE_URL, data=json.dumps({"pack_code": "credits-500"}), content_type="application/json"
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_purchase_returns_a_checkout_url(auth_client, workspace, packs) -> None:
    response = auth_client.post(PURCHASE_URL, {"pack_code": "credits-500"}, format="json")

    assert response.status_code == 200
    assert response.json()["checkout_url"].startswith("https://checkout.test/")


def test_purchase_rejects_a_missing_pack_code(auth_client, workspace, packs) -> None:
    response = auth_client.post(PURCHASE_URL, {}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["detail"]["fields"]["pack_code"]


def test_a_402_carries_the_upgrade_payload(auth_client, workspace, packs) -> None:
    """design.md A2 — a 402 is the frontend's signal to render an upgrade
    prompt, and it is never raised without somewhere to send the user."""
    response = auth_client.post(PURCHASE_URL, {"pack_code": "videos-4"}, format="json")

    assert response.status_code == 402
    assert response.json()["error"]["upgrade"] == {"suggested_plan": "pro", "cta": "/app/billing"}


def test_packs_are_listed_for_a_signed_in_workspace(auth_client, workspace, packs) -> None:
    response = auth_client.get(PACKS_URL)

    assert response.status_code == 200
    codes = [row["code"] for row in response.json()]
    assert codes == ["credits-500", "videos-4"]
    assert response.json()[0]["units"] == 500


def test_withdrawn_packs_are_not_listed(auth_client, workspace, packs) -> None:
    pack = packs["videos-4"]
    pack.is_public = False
    pack.save()

    codes = [row["code"] for row in auth_client.get(PACKS_URL).json()]
    assert codes == ["credits-500"]


def test_packs_need_a_session(client, packs) -> None:
    assert client.get(PACKS_URL).status_code == 401
