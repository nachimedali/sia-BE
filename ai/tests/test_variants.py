"""Studio variant economics (X-09).

A generation renders a **pool** and the buyer pays for a smaller number of
**slots**. What these pin is the boundary between the two: how many variants
are free, what the surplus costs, and that nothing reaches a platform on the
way out.

The pricing change is deliberate and recorded. Before X-09 one generation
action cost one price whatever `n` was asked for (design.md A73); it is now
priced per slot, and `test_flag_off_restores_one_charge_per_action` is what
keeps the old behaviour reachable rather than merely remembered.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.models import (
    Generation,
    GenerationCost,
    GenerationKind,
    GenerationMode,
    GenerationStatus,
)
from ai.services import variants as variant_service
from ai.services.costing import resolve_pricing, unlock_price
from ai.services.pipeline import create_generation, run_generation
from billing.models import FeatureFlag
from billing.services.flags import STUDIO_VARIANTS_V2
from billing.services.ledger import credit_balance, grant_credits
from common.exceptions import Purchasable, StateConflict

pytestmark = pytest.mark.django_db


@pytest.fixture
def funded(workspace: Any, plans: Any, generation_costs: None) -> Any:
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan", "updated_at"])
    grant_credits(workspace, 500, note="test funding")
    return workspace


def _generated(workspace: Any, user: Any, *, slots: int = 2) -> Generation:
    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning",
        paid_slots=slots,
    )
    return run_generation(generation, n=1)


def _price(generation: Generation) -> int:
    return resolve_pricing(kind=generation.kind, mode=generation.mode).credits


# -----------------------------------------------------------------------------
# Pricing — the change itself
# -----------------------------------------------------------------------------
def test_paying_for_n_slots_costs_n_times_one_variant(funded: Any, user: Any) -> None:
    before = credit_balance(funded)
    generation = _generated(funded, user, slots=3)

    assert generation.status == GenerationStatus.SUCCEEDED
    assert generation.credits_charged == _price(generation) * 3
    assert credit_balance(funded) == before - generation.credits_charged


def test_the_engine_renders_more_than_was_bought(funded: Any, user: Any) -> None:
    """There is nothing to upsell unless the surplus already exists."""
    generation = _generated(funded, user, slots=1)
    pool = resolve_pricing(kind=generation.kind, mode=generation.mode).variant_pool

    assert generation.variants.count() == pool
    assert pool > generation.paid_slots


def test_the_pool_never_renders_fewer_than_the_slots_bought(funded: Any, user: Any) -> None:
    """A buyer who paid for six must be able to choose six, whatever the
    pool column says."""
    pricing = resolve_pricing(kind=GenerationKind.TEXT, mode=GenerationMode.IDEA)
    generation = _generated(funded, user, slots=pricing.variant_pool + 2)

    assert generation.variants.count() == pricing.variant_pool + 2


def test_flag_off_restores_one_charge_per_action(funded: Any, user: Any) -> None:
    """design.md A73's original terms, still reachable — this flag gates a
    price change, which is the only reason it exists."""
    FeatureFlag.objects.create(
        organization=funded.organization, key=STUDIO_VARIANTS_V2, enabled=False
    )
    before = credit_balance(funded)
    generation = _generated(funded, user, slots=4)

    assert generation.paid_slots == 1
    assert generation.credits_charged == _price(generation)
    assert credit_balance(funded) == before - _price(generation)


@pytest.mark.parametrize(
    ("credits", "percent", "expected"),
    [
        # Half of three is two. Rounding down would sell the surplus for less
        # than half — the kind of detail nobody notices until the margin does.
        (3, 50, 2),
        (1, 50, 1),
        (4, 50, 2),
        (10, 50, 5),
        # Never free: an operator who wants them given away sets the pool to
        # the slot count instead of the percentage to nothing.
        (3, 0, 1),
    ],
)
def test_the_unlock_price_rounds_up_and_is_never_free(
    credits: int, percent: int, expected: int
) -> None:
    assert unlock_price(GenerationCost(credits=credits, unlock_percent=percent)) == expected


# -----------------------------------------------------------------------------
# Selection — the allowance
# -----------------------------------------------------------------------------
def test_selecting_within_the_allowance_is_free(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=2)
    ids = [v.pk for v in generation.variants.all()[:2]]
    before = credit_balance(funded)

    chosen = variant_service.select(generation, variant_ids=ids, actor=user)

    assert {v.pk for v in chosen} == set(ids)
    assert credit_balance(funded) == before


def test_selecting_past_the_allowance_is_402_carrying_the_price(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=1)
    ids = [v.pk for v in generation.variants.all()[:3]]

    with pytest.raises(Purchasable) as caught:
        variant_service.select(generation, variant_ids=ids, actor=user)

    assert caught.value.code == "variant_allowance_exceeded"
    detail = caught.value.payload
    assert detail["unlock_required"] == 2
    assert detail["unlock_total"] == detail["unlock_price"] * 2


def test_selection_replaces_rather_than_accumulates(funded: Any, user: Any) -> None:
    """The dock sends its whole answer, so a deselected variant really is
    deselected — a toggle would leave the two ends disagreeing about a click
    that did not arrive."""
    generation = _generated(funded, user, slots=1)
    first, second = list(generation.variants.all()[:2])

    variant_service.select(generation, variant_ids=[first.pk], actor=user)
    variant_service.select(generation, variant_ids=[second.pk], actor=user)

    assert list(generation.variants.filter(was_selected=True)) == [second]


def test_another_generations_variant_is_not_selectable(funded: Any, user: Any) -> None:
    mine = _generated(funded, user, slots=1)
    theirs = _generated(funded, user, slots=1)
    stranger = theirs.variants.all()[0]

    with pytest.raises(variant_service.VariantNotSelectableError):
        variant_service.select(mine, variant_ids=[stranger.pk], actor=user)


# -----------------------------------------------------------------------------
# Unlocking — the upsell
# -----------------------------------------------------------------------------
def test_unlocking_debits_half_and_frees_the_slot(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=1)
    pricing = resolve_pricing(kind=generation.kind, mode=generation.mode)
    first, second = list(generation.variants.all()[:2])
    before = credit_balance(funded)

    variant_service.unlock(generation, variant_ids=[second.pk], actor=user)

    assert credit_balance(funded) == before - unlock_price(pricing)
    # Both are now selectable although only one slot was ever bought.
    chosen = variant_service.select(generation, variant_ids=[first.pk, second.pk], actor=user)
    assert len(chosen) == 2


def test_unlocking_the_same_variant_twice_charges_once(funded: Any, user: Any) -> None:
    """The retry after a dropped response is indistinguishable from a second
    click, and only one of them should cost money."""
    generation = _generated(funded, user, slots=1)
    variant = generation.variants.all()[1]

    variant_service.unlock(generation, variant_ids=[variant.pk], actor=user)
    after_first = credit_balance(funded)
    again = variant_service.unlock(generation, variant_ids=[variant.pk], actor=user)

    assert again == []
    assert credit_balance(funded) == after_first


def test_what_was_charged_is_recorded_on_the_variant(funded: Any, user: Any) -> None:
    """A price retuned in admin must not rewrite what a customer paid."""
    generation = _generated(funded, user, slots=1)
    pricing = resolve_pricing(kind=generation.kind, mode=generation.mode)
    variant = generation.variants.all()[1]

    variant_service.unlock(generation, variant_ids=[variant.pk], actor=user)
    variant.refresh_from_db()

    assert variant.unlock_charged == unlock_price(pricing)


def test_a_generation_made_without_a_pool_has_nothing_to_unlock(funded: Any, user: Any) -> None:
    """**The row carries the terms, not the org's flag today.**

    Refused because *this generation* was created with no surplus — not
    because the flag happens to be off at the moment of the click. The
    difference is what stops a flag toggled between generation and review from
    taking away something already paid for.
    """
    FeatureFlag.objects.create(
        organization=funded.organization, key=STUDIO_VARIANTS_V2, enabled=False
    )
    generation = _generated(funded, user, slots=1)

    with pytest.raises(StateConflict):
        variant_service.unlock(
            generation, variant_ids=[generation.variants.all()[0].pk], actor=user
        )


def test_a_flag_flipped_after_purchase_does_not_take_the_pool_away(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=1)
    FeatureFlag.objects.create(
        organization=funded.organization, key=STUDIO_VARIANTS_V2, enabled=False
    )

    # Bought under X-09's terms, so the surplus stays buyable.
    unlocked = variant_service.unlock(
        generation, variant_ids=[generation.variants.all()[1].pk], actor=user
    )
    assert len(unlocked) == 1


# -----------------------------------------------------------------------------
# Commit — onto the calendar, never past the human
# -----------------------------------------------------------------------------
def test_committing_makes_one_draft_per_selected_variant(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=2)
    ids = [v.pk for v in generation.variants.all()[:2]]
    variant_service.select(generation, variant_ids=ids, actor=user)

    posts = variant_service.commit(generation, actor=user)

    assert len(posts) == 2
    # Drafts. Nothing scheduled, nothing published, no target built.
    for post in posts:
        assert post.status == "DRAFT"
        assert post.generation_id == generation.pk


def test_committing_nothing_is_a_409_not_an_empty_success(funded: Any, user: Any) -> None:
    generation = _generated(funded, user, slots=2)

    with pytest.raises(StateConflict):
        variant_service.commit(generation, actor=user)


def test_a_pending_generation_has_nothing_to_act_on(funded: Any, user: Any) -> None:
    pending = create_generation(
        workspace=funded,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="not run yet",
        paid_slots=1,
    )

    for call in (
        lambda: variant_service.select(pending, variant_ids=[], actor=user),
        lambda: variant_service.unlock(pending, variant_ids=[], actor=user),
        lambda: variant_service.commit(pending, actor=user),
    ):
        with pytest.raises(StateConflict):
            call()
