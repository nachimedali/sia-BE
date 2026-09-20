"""What a workspace still has to do before autopilot can draft (X-08).

Autopilot has six preconditions spread across five subsystems — plan, product,
taste, config, credits, channels — and every one of them fails the same way:
the queue renders empty. That is indistinguishable from "autopilot ran and
found nothing to say", which is why this module exists: it answers *which* of
the six is missing, from the same code the engine itself checks.

**Each row is derived, never stored.** A `setup_complete` column would be a
second source of truth for something six other tables already answer, and it
would be the one that went stale — the workspace that disconnects its last
channel must stop reading as ready the moment it does, not at the next write.

Ordered by what a user does first: pay, register a product, describe the
brand, switch it on. `channel` is last and non-blocking — drafting works
without one; only approval does not (`build_targets` raises
`NoConnectedAccountsError`), and learning that at approval time is a dead end.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.utils import timezone

from ai.models import GenerationKind, GenerationMode
from ai.services.costing import GenerationCostNotConfiguredError, resolve_cost
from billing.services.entitlements import entitlements_for
from channels.services import active_accounts
from common.setup import Requirement, done, is_ready, missing
from products.models import AutopilotConfig, Product
from products.services import autopilot
from taste.models import TasteProfile
from taste.services import profiles as profile_service
from workspaces.models import Workspace

#: How many slots to name once setup is complete. Three is enough to show the
#: rhythm — a full fortnight of dates reads as a wall of text.
SLOT_PREVIEW = 3


def _plan_row(entitlements: Any) -> Requirement:
    if entitlements.feature("autopilot"):
        return done("plan", entitlements.plan.display_name)
    return missing("plan", entitlements.plan.display_name)


def _product_row(workspace: Workspace) -> Requirement:
    products = list(Product.objects.filter(workspace=workspace))
    ready = [product for product in products if product.is_generation_ready]
    facts = {"total": len(products), "ready": len(ready)}

    if ready:
        return done("product", f"{len(ready)} of {len(products)} ready to generate", facts=facts)
    return missing(
        "product",
        (
            "No products yet"
            if not products
            else f"{len(products)} registered, none with a reference image"
        ),
        target_id=products[0].pk if products else None,
        facts=facts,
    )


def _taste_row(workspace: Workspace) -> Requirement:
    """An inactive saved version is one click from done, so it is offered for
    activation rather than sent back through the editor — the same distinction
    `run_config` makes when it refuses a run with `no_taste_profile`."""
    active = profile_service.active_profile(workspace)
    if active is not None:
        return done("taste_profile", f"Version {active.version} active", target_id=active.pk)

    latest = TasteProfile.objects.filter(workspace=workspace).order_by("-version").first()
    if latest is not None:
        return missing(
            "taste_profile",
            f"Version {latest.version} saved, not active",
            target_id=latest.pk,
            facts={"version": latest.version},
        )
    return missing("taste_profile", "No profile yet")


def _autopilot_row(workspace: Workspace) -> Requirement:
    """The cadence lives on this row rather than on a seventh card: switching
    autopilot on *is* choosing a rhythm, and a card that only said "on" would
    hide the one number the user came to check."""
    config = (
        AutopilotConfig.objects.filter(product__workspace=workspace, enabled=True)
        .select_related("product")
        .order_by("pk")
        .first()
    )
    if config is not None:
        return done(
            "autopilot",
            f"{config.product.name}: every {config.cadence_days} days, "
            f"{config.lookahead_days} days ahead",
            target_id=config.product_id,
            facts={
                "cadence_days": config.cadence_days,
                "lookahead_days": config.lookahead_days,
                "enabled_products": AutopilotConfig.objects.filter(
                    product__workspace=workspace, enabled=True
                ).count(),
            },
        )

    candidate = next(
        (
            product
            for product in Product.objects.filter(workspace=workspace).order_by("pk")
            if product.is_generation_ready
        ),
        None,
    )
    return missing(
        "autopilot",
        "Off on every product",
        target_id=candidate.pk if candidate else None,
        facts={"enabled_products": 0},
    )


def _per_slot_cost() -> int | None:
    """What one drafted slot costs — an image plus a caption — or `None` when
    the cost table has not been seeded.

    Priced from the table rather than a constant, because Part 7 rule 10 puts
    both numbers in a row an operator can edit. **The `None` is the point:**
    this endpoint exists so an unconfigured workspace gets guidance instead of
    an error, and an environment missing `seed_generation_costs` is exactly
    such a workspace — letting `resolve_cost` raise here would answer the
    setup screen with a 400 and tell the user nothing at all. Null rather than
    zero, for the reason rule 12 gives about metrics: a price we do not know
    is not a slot that is free.
    """
    try:
        image = resolve_cost(kind=GenerationKind.IMAGE, mode=GenerationMode.AUTOPILOT)
        caption = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.AUTOPILOT)
    except GenerationCostNotConfiguredError:
        return None
    return image + caption


def _credits_row(workspace: Workspace) -> Requirement:
    per_slot = _per_slot_cost()
    balance = entitlements_for(workspace).credits_remaining()
    facts: dict[str, Any] = {"balance": balance, "per_slot": per_slot}

    # With no price to compare against, an empty balance is the only shortfall
    # that can be asserted — claiming any other is a guess.
    funded = balance > 0 if per_slot is None else balance >= per_slot
    return (done if funded else missing)("credits", f"{balance} credits", facts=facts)


def _channel_row(workspace: Workspace) -> Requirement:
    accounts = list(active_accounts(workspace))
    facts = {"connected": len(accounts)}
    if accounts:
        return done(
            "channel",
            ", ".join(sorted({account.platform for account in accounts})),
            blocking=False,
            facts=facts,
        )
    return missing("channel", "No channel connected", blocking=False, facts=facts)


def autopilot_requirements(workspace: Workspace) -> list[Requirement]:
    entitlements = entitlements_for(workspace)
    return [
        _plan_row(entitlements),
        _product_row(workspace),
        _taste_row(workspace),
        _autopilot_row(workspace),
        _credits_row(workspace),
        _channel_row(workspace),
    ]


def next_slots(workspace: Workspace, *, now: dt.datetime | None = None) -> list[dt.datetime]:
    """The engine's own grid, not a recomputation of it.

    "When does something appear?" is the question a finished setup screen
    leaves behind, and answering it from `planned_slots` means the dates on
    screen are the dates that will be drafted — including after a cadence
    change, which shifts both together or neither.
    """
    now = now or timezone.now()
    config = (
        AutopilotConfig.objects.filter(product__workspace=workspace, enabled=True)
        .select_related("product")
        .order_by("pk")
        .first()
    )
    if config is None:
        return []
    entitlements = entitlements_for(workspace)
    horizon = autopilot._horizon_days(config, entitlements)
    return autopilot.planned_slots(config, now=now, horizon_days=horizon)[:SLOT_PREVIEW]


def autopilot_readiness(workspace: Workspace) -> dict[str, Any]:
    requirements = autopilot_requirements(workspace)
    return {
        "ready": is_ready(requirements),
        "requirements": requirements,
        "next_slots": next_slots(workspace),
    }
