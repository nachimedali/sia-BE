"""Buy on the fly: a 402 that can be cleared without leaving the screen.

Running out of credits does not need a different plan — it needs ten dollars.
Sending someone to a pricing page to spend it turns a thirty-second purchase
into an abandoned session, so the packs that would unblock *this* request ride
along on the error, priced in the organization's own currency.

Two properties matter more than the feature itself: the offer must never be the
reason a 402 fails, and it must never appear when there is nothing to sell.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from billing.models import Currency, Pack, PackKind, PackPrice
from billing.services.entitlements import entitlements_for
from common.exceptions import InsufficientCredits, InsufficientVideoUnits

pytestmark = pytest.mark.django_db

GENERATE_URL = "/api/v1/ai/generate/"


@pytest.fixture
def catalogue(db: None) -> None:
    call_command("seed_currencies", verbosity=0)
    call_command("seed_packs", verbosity=0)


# -----------------------------------------------------------------------------
# The offer
# -----------------------------------------------------------------------------
def test_running_out_of_credits_offers_the_packs_that_fix_it(
    workspace: Any, catalogue: None
) -> None:
    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    offers = caught.value.purchase
    assert offers, "a blocked generation should carry something to buy"
    assert all(offer["units"] > 0 for offer in offers)
    assert all("display" in offer for offer in offers)


def test_only_the_matching_kind_is_offered(workspace: Any, catalogue: None) -> None:
    """Offering a video pack to someone out of text credits is noise at exactly
    the moment they are least patient with it."""
    credit_codes = {
        pack.code for pack in Pack.objects.filter(kind=PackKind.CREDITS, is_public=True)
    }

    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    assert {offer["code"] for offer in caught.value.purchase} == credit_codes


def test_video_exhaustion_offers_video_packs(workspace: Any, plans: Any, catalogue: None) -> None:
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])

    with pytest.raises(InsufficientVideoUnits) as caught:
        entitlements_for(workspace).require_video_units(999)

    kinds = {Pack.objects.get(code=offer["code"]).kind for offer in caught.value.purchase}
    assert kinds == {PackKind.VIDEO}


def test_the_offer_is_priced_in_the_organizations_currency(
    workspace: Any, organization: Any, catalogue: None
) -> None:
    """Per-country pricing reaches the moment of the block, not just the
    pricing page — quoting dollars to a customer billed in dirhams is how a
    purchase gets abandoned at the last step."""
    mad = Currency.objects.get(code="MAD")
    for pack in Pack.objects.filter(kind=PackKind.CREDITS):
        PackPrice.objects.create(pack=pack, currency=mad, amount_cents=10000)
    organization.billing_currency = mad
    organization.save(update_fields=["billing_currency"])

    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    assert all(offer["currency"] == "MAD" for offer in caught.value.purchase)
    assert caught.value.purchase[0]["display"].endswith("DH")


def test_a_withdrawn_pack_is_not_offered(workspace: Any, catalogue: None) -> None:
    Pack.objects.filter(kind=PackKind.CREDITS).update(is_public=False)

    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    assert caught.value.purchase == []


def test_nothing_on_sale_still_raises_the_402(workspace: Any) -> None:
    """No packs seeded at all. The block is the point; the offer is a
    courtesy, and an empty one must not become an empty buy button."""
    assert not Pack.objects.exists()

    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    assert caught.value.status_code == 402
    assert caught.value.purchase == []
    # The upgrade path is still there — it is what remains when nothing is on
    # sale that would clear this.
    assert caught.value.upgrade["suggested_plan"]


def test_a_broken_catalogue_never_turns_a_402_into_a_500(
    workspace: Any, catalogue: None, monkeypatch: Any
) -> None:
    """ "You need more credits" is a strictly better answer than a server
    error, so the offer is allowed to fail and the block is not."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("catalogue is down")

    monkeypatch.setattr("billing.services.pricing.purchase_options", explode)

    with pytest.raises(InsufficientCredits) as caught:
        entitlements_for(workspace).require_credits(999)

    assert caught.value.purchase == []


# -----------------------------------------------------------------------------
# …and it reaches the client
# -----------------------------------------------------------------------------
def test_the_402_envelope_carries_the_offer(
    auth_client: Any, workspace: Any, catalogue: None, generation_costs: None
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {"kind": "TEXT", "mode": "IDEA", "prompt": "a new mug glaze"},
        format="json",
    )

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "insufficient_credits"
    # Both paths, side by side: buy what clears this, or move up a plan.
    assert error["purchase"]
    assert error["upgrade"]


def test_the_envelope_omits_purchase_when_there_is_nothing_to_sell(
    auth_client: Any, workspace: Any, generation_costs: None
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {"kind": "TEXT", "mode": "IDEA", "prompt": "a new mug glaze"},
        format="json",
    )

    assert response.status_code == 402
    assert "purchase" not in response.json()["error"]
