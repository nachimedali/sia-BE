"""`GenerationCost` resolution (design.md §4.2, A10).

Resolution order: exact `(kind, mode, provider, model)` → `(kind, mode)` →
`(kind)`. No match is a hard error, never a silent zero — a missing cost row
must never let a generation debit nothing, which is why this is the *only*
reader of the table (design.md §6.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ai.models import GenerationCost, GenerationKind
from common.exceptions import OCCSError

if TYPE_CHECKING:
    from workspaces.models import Workspace


class GenerationCostNotConfiguredError(OCCSError):
    default_code = "generation_cost_not_configured"
    default_detail = "No credit cost is configured for this generation."


def preflight_require_credits(workspace: Workspace, *, kind: str, mode: str = "") -> None:
    """Raises if `workspace` cannot afford `(kind, mode)` — the shared shape
    behind I5's serializer and DRF-permission gates (design.md §8.1). Video is
    priced through `VideoLedger` (§4.3), not this credit-cost table, so it is
    a no-op here; `create_generation`'s feature check is what gates it."""
    from billing.services.entitlements import entitlements_for

    if kind == GenerationKind.VIDEO:
        return
    cost = resolve_cost(kind=kind, mode=mode)
    entitlements_for(workspace).require_credits(cost)


def resolve_pricing(
    *, kind: str, mode: str = "", provider: str = "", model: str = ""
) -> GenerationCost:
    """The whole priced row, not just its credit figure (X-09).

    Variant economics need three numbers off the same row — the per-variant
    price, how many the engine renders, and what a surplus one costs to
    unlock — and resolving them through three separate lookups would let a
    generation be priced by one row and pooled by another the instant the
    fallback chain disagreed. One resolution, one row, one set of terms.
    """
    for filters in (
        {"kind": kind, "mode": mode, "provider": provider, "model": model},
        {"kind": kind, "mode": mode, "provider": "", "model": ""},
        {"kind": kind, "mode": "", "provider": "", "model": ""},
    ):
        row = GenerationCost.objects.filter(is_active=True, **filters).first()
        if row is not None:
            return row
    raise GenerationCostNotConfiguredError(
        detail={"kind": kind, "mode": mode, "provider": provider, "model": model}
    )


def resolve_cost(*, kind: str, mode: str = "", provider: str = "", model: str = "") -> int:
    """The price of **one** variant.

    Unchanged as a lookup; what changed around it is how many times a caller
    multiplies it (X-09). Kept as the narrow entry point because most callers
    want exactly this and should not have to hold a model instance to get it.
    """
    return resolve_pricing(kind=kind, mode=mode, provider=provider, model=model).credits


def unlock_price(row: GenerationCost) -> int:
    """What one surplus variant costs, rounded **up**.

    Up, not nearest: half of a 3-credit image is 1.5, and rounding that down
    would sell the surplus for less than half — a rounding rule that quietly
    discounts is the kind of commercial detail nobody notices until the
    margin does. Never free either, so a 0% row still costs 1; an operator who
    wants them given away sets `variant_pool` to the slot count instead.
    """
    return max(1, -(-row.credits * row.unlock_percent // 100))
