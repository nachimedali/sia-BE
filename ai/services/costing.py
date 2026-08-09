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


def resolve_cost(*, kind: str, mode: str = "", provider: str = "", model: str = "") -> int:
    for filters in (
        {"kind": kind, "mode": mode, "provider": provider, "model": model},
        {"kind": kind, "mode": mode, "provider": "", "model": ""},
        {"kind": kind, "mode": "", "provider": "", "model": ""},
    ):
        row = GenerationCost.objects.filter(is_active=True, **filters).first()
        if row is not None:
            return row.credits
    raise GenerationCostNotConfiguredError(
        detail={"kind": kind, "mode": mode, "provider": provider, "model": model}
    )
