"""Variant selection, unlocking and commitment (X-09).

A Studio generation renders a **pool** of variants and the buyer pays for a
smaller number of **slots**. The surplus is real, rendered and visible, and
costs `GenerationCost.unlock_percent` of one slot's price to keep. Three verbs
separate the concerns:

* `select` — mark which variants the user wants. Refuses past the allowance
  with a 402 naming what it would cost to go further, so the upsell is a fact
  the client reads off the error rather than a price it computes itself.
* `unlock` — buy a surplus variant. Debits once, records what was charged, and
  is idempotent per variant: paying twice for the same picture is the one
  outcome this module exists to prevent.
* `commit` — turn the selection into draft posts on the calendar.

**The allowance is a count, not a set.** Which variants are free is the user's
choice and changes as they click; what cannot change is how many. An unlocked
variant stops counting against the allowance, which is what lets "only N free"
and "the rest are buyable" coexist without either rule knowing about the other.

**Nothing here publishes.** `commit` creates drafts through `create_post`, the
ordinary authoring path, and schedules them through `schedule_post`, which is
the sole writer of `delivery_mode`/`scheduled_at` and applies the horizon,
quota and approval gates. A post leaving this module is awaiting review exactly
as one typed by hand would be (L-2).
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction

from accounts.models import User
from ai.models import Generation, GenerationKind, GenerationStatus, GenerationVariant
from ai.services.costing import resolve_pricing, unlock_price
from billing.models import CreditReason
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError, Purchasable, StateConflict
from content.models import Post
from content.services.posts import create_post, update_post
from scheduling.services import default_delivery_mode, schedule_post


class VariantNotSelectableError(OCCSError):
    default_code = "variant_not_selectable"
    default_detail = "That variant cannot be selected."


def _variants(generation: Generation, variant_ids: list[int]) -> list[GenerationVariant]:
    """Resolved **through the generation**, never by bare id.

    A variant id from another workspace must not be findable here, and scoping
    the lookup is what makes that structural rather than a check someone
    remembers to write. The caller has already proved it owns the generation.
    """
    found = list(generation.variants.filter(pk__in=variant_ids))
    missing = set(variant_ids) - {variant.pk for variant in found}
    if missing:
        raise VariantNotSelectableError(
            "No such variant on this generation.", detail={"variants": sorted(missing)}
        )
    return found


def _offers_surplus(generation: Generation) -> bool:
    """Whether X-09's terms apply to **this row**, not to the org right now.

    `variant_pool` is non-zero only on a generation created under the flag, so
    the row already records the answer — and reading it rather than
    `flag_enabled` is what stops a flag toggled between generation and review
    from changing the terms something was already paid for. Same reasoning as
    storing the pool in the first place: a retuned column must not rewrite what
    an old generation is understood to have offered.
    """
    return generation.variant_pool > 0


def _require_succeeded(generation: Generation) -> None:
    if generation.status != GenerationStatus.SUCCEEDED:
        raise StateConflict(
            "This generation has no variants to act on.",
            detail={"generation": generation.pk, "status": generation.status},
        )


@transaction.atomic
def select(
    generation: Generation, *, variant_ids: list[int], actor: User
) -> list[GenerationVariant]:
    """Set the selection to exactly `variant_ids`.

    A replace rather than a toggle: the dock is a set of checkboxes and the
    user's final answer is the whole set, so sending it whole removes any
    question of what happened to a click that did not arrive.

    Only the **allowance check** is conditional on X-09 applying to this row;
    applying the selection is the same work either way, and writing it twice
    would be two places to fix the next time it changes.
    """
    _require_succeeded(generation)
    chosen = _variants(generation, variant_ids)

    if _offers_surplus(generation):
        needs_slot = [variant for variant in chosen if not variant.is_unlocked]
        if len(needs_slot) > generation.paid_slots:
            pricing = resolve_pricing(kind=generation.kind, mode=generation.mode)
            over = len(needs_slot) - generation.paid_slots
            # **`Purchasable`, not a plan 402.** The distinction is what can be
            # done about it: this customer may already be on the top plan, and
            # sending them to a pricing page to buy a picture they are looking
            # at is how a five-second purchase becomes an abandoned session.
            # The error carries the price so the client never computes it.
            raise Purchasable(
                "That is more variants than this generation paid for.",
                code="variant_allowance_exceeded",
                detail={
                    "paid_slots": generation.paid_slots,
                    "selected": len(needs_slot),
                    "unlock_required": over,
                    "unlock_price": unlock_price(pricing),
                    "unlock_total": unlock_price(pricing) * over,
                },
            )

    picked = {variant.pk for variant in chosen}
    generation.variants.update(was_selected=False)
    generation.variants.filter(pk__in=picked).update(was_selected=True)
    # Reflected in Python rather than re-read: the two updates above already
    # say what every flag now is, and a third query to be told so is a round
    # trip for an answer we just wrote.
    for variant in chosen:
        variant.was_selected = True
    return chosen


@transaction.atomic
def unlock(
    generation: Generation, *, variant_ids: list[int], actor: User
) -> list[GenerationVariant]:
    """Buy surplus variants at `unlock_percent` of one slot's price.

    Idempotent per variant: an already-unlocked one is skipped rather than
    charged again, because the retry that arrives after a dropped response is
    indistinguishable from a second click and only one of them should cost
    money. Priced once for the whole call so a two-variant purchase cannot be
    split across a price change mid-loop.
    """
    _require_succeeded(generation)
    if not _offers_surplus(generation):
        raise StateConflict(
            "This generation was not created with a variant pool to unlock from.",
            detail={"generation": generation.pk},
        )

    pricing = resolve_pricing(kind=generation.kind, mode=generation.mode)
    price = unlock_price(pricing)
    chosen = [variant for variant in _variants(generation, variant_ids) if not variant.is_unlocked]
    if not chosen:
        return []

    workspace = generation.workspace
    entitlements = entitlements_for(workspace)
    # One debit for the whole purchase: the ledger row a customer reads should
    # say "unlocked 2 variants", not appear twice for one action.
    ledger.debit_credits(
        workspace,
        price * len(chosen),
        reason=CreditReason.GENERATION,
        quota=entitlements.quota("monthly_ai_credits"),
        note=f"unlocked {len(chosen)} variant(s) of generation {generation.pk}",
        generation=generation,
    )
    for variant in chosen:
        variant.is_unlocked = True
        variant.unlock_charged = price
    GenerationVariant.objects.bulk_update(chosen, ["is_unlocked", "unlock_charged"])
    return chosen


@transaction.atomic
def commit(
    generation: Generation, *, actor: User, scheduled_at: dt.datetime | None = None
) -> list[Post]:
    """Turn the selection into draft posts, one per selected variant.

    One post per variant rather than one post carrying them all: the user
    picked several *pictures*, and a single post with four images is a
    carousel, which is a different thing they did not ask for. Each lands as
    an ordinary draft, and — when a time is given — goes through
    `schedule_post`, which is the only writer of the schedule and the place the
    approval chain is applied. Nothing here reaches a platform.
    """
    _require_succeeded(generation)
    selected = list(generation.variants.filter(was_selected=True).select_related("media_asset"))
    if not selected:
        raise StateConflict(
            "Nothing is selected on this generation.", detail={"generation": generation.pk}
        )

    # Resolved once, outside the loop: it depends on the workspace, not on the
    # variant, and `entitlements_for` builds a fresh object (and a Redis read)
    # on every call — twelve selected variants would have meant twelve.
    mode = default_delivery_mode(generation.workspace) if scheduled_at is not None else ""

    posts: list[Post] = []
    for variant in selected:
        post = create_post(
            workspace=generation.workspace,
            author=actor,
            master_body=variant.body if variant.kind == GenerationKind.TEXT else "",
            category=generation.category,
            media_assets=[variant.media_asset] if variant.media_asset else [],
        )
        # `product`/`generation`/`source` are read-only on the serializer (A49)
        # so that only the service that owns them writes them.
        update_post(
            post,
            reason="studio",
            product=generation.product,
            generation=generation,
        )
        if scheduled_at is not None:
            schedule_post(post=post, delivery_mode=mode, scheduled_at=scheduled_at, actor=actor)
        posts.append(post)
    return posts
