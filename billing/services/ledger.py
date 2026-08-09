"""The two ledgers (design.md §8.2, I4).

Balance is `SUM(delta)`. `balance_after` is a per-row cache for display and for
the nightly reconciliation to check against — it is never read to decide whether
a spend is allowed, because a stale cache would authorise a spend the balance
does not cover.

**Every mutation goes through this module.** There is no update or delete path
anywhere, by design: a correction is a compensating row, so the history of what
was charged and refunded survives the correction.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.db.models import Sum

from billing.models import (
    UNLIMITED,
    CreditLedger,
    CreditReason,
    VideoLedger,
    VideoReason,
)
from common.exceptions import InsufficientCredits, InsufficientVideoUnits
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

LedgerModel = type[CreditLedger] | type[VideoLedger]


def _balance(model: LedgerModel, workspace: Workspace) -> int:
    total = model.objects.filter(workspace=workspace).aggregate(total=Sum("delta"))["total"]
    return int(total or 0)


def credit_balance(workspace: Workspace) -> int:
    return _balance(CreditLedger, workspace)


def video_balance(workspace: Workspace) -> int:
    return _balance(VideoLedger, workspace)


def _lock_workspace(workspace: Workspace) -> None:
    """Serialises concurrent spends for one workspace.

    The row lock is the whole reason two requests cannot both spend the last
    credit: without it, both read a balance of 1, both pass the check, and both
    insert. Locking the workspace rather than the ledger rows also covers the
    first spend, when there are no rows to lock.
    """
    Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()


# PEP 695 syntax. Constrained rather than bound: the two ledgers share
# `LedgerEntry`, but these helpers must hand back the concrete class they were
# given, not the base.
def _append[LedgerT: (CreditLedger, VideoLedger)](
    model: type[LedgerT],
    workspace: Workspace,
    *,
    delta: int,
    **fields: Any,
) -> LedgerT:
    balance = _balance(model, workspace)
    entry: LedgerT = model.objects.create(
        workspace=workspace, delta=delta, balance_after=balance + delta, **fields
    )
    return entry


# -----------------------------------------------------------------------------
# Credits
# -----------------------------------------------------------------------------
@transaction.atomic
def debit_credits(
    workspace: Workspace,
    cost: int,
    *,
    reason: str = CreditReason.GENERATION,
    quota: int,
    note: str = "",
    generation: Any = None,
) -> CreditLedger:
    """Spends `cost` credits, or raises 402 (design.md §8.2).

    `quota` comes from the caller's already-resolved `Entitlements`, so this
    module never reaches back into plan resolution — the resolver is the one
    place that knows about trials and overrides.

    An unlimited plan still writes a row, with `delta=0`: usage telemetry must
    not go dark on the top tier (§4.1).
    """
    if cost < 0:
        raise ValueError("Use refund_credits() to return credits, not a negative debit.")

    _lock_workspace(workspace)
    balance = _balance(CreditLedger, workspace)

    unlimited = quota == UNLIMITED
    if not unlimited and balance < cost:
        raise InsufficientCredits(
            f"This action needs {cost} credits; {balance} remaining.",
            detail={"required": cost, "available": balance},
        )

    entry = _append(
        CreditLedger,
        workspace,
        delta=0 if unlimited else -cost,
        reason=reason,
        note=note or (f"unlimited plan; nominal cost {cost}" if unlimited else ""),
        generation=generation,
    )
    logger.info(
        "credits debited",
        extra={"workspace_id": workspace.pk, "cost": cost, "balance_after": entry.balance_after},
    )
    return entry


@transaction.atomic
def refund_credits(entry: CreditLedger, *, note: str = "") -> CreditLedger:
    """Writes the compensating row for a debit. Never an update, never a delete.

    Idempotent on the debit: a second refund of the same entry returns the row
    already written, so a retried task cannot hand back the credits twice.
    """
    existing = CreditLedger.objects.filter(reverses=entry, reason=CreditReason.REFUND).first()
    if existing is not None:
        return existing

    _lock_workspace(entry.workspace)
    return _append(
        CreditLedger,
        entry.workspace,
        delta=-entry.delta,
        reason=CreditReason.REFUND,
        reverses=entry,
        generation=entry.generation,
        note=note or "quality gate failed",
    )


@transaction.atomic
def grant_credits(
    workspace: Workspace,
    amount: int,
    *,
    reason: str = CreditReason.MONTHLY_GRANT,
    rollover: bool = False,
    actor: Any = None,
    note: str = "",
) -> list[CreditLedger]:
    """Grants a period's credits.

    Credits do not roll over unless the plan says they do (§4.1), so the grant
    writes a zeroing row first. Two rows rather than one net figure, because
    "you were given 150 and 20 expired" is the answer a billing question needs.
    """
    _lock_workspace(workspace)
    rows: list[CreditLedger] = []

    if not rollover:
        balance = _balance(CreditLedger, workspace)
        if balance > 0:
            rows.append(
                _append(
                    CreditLedger,
                    workspace,
                    delta=-balance,
                    reason=reason,
                    note="expired: credits do not roll over",
                )
            )

    rows.append(
        _append(
            CreditLedger,
            workspace,
            delta=amount,
            reason=reason,
            actor=actor,
            note=note,
        )
    )
    return rows


@transaction.atomic
def purchase_credits(workspace: Workspace, amount: int, *, note: str) -> CreditLedger:
    """Adds bought credits. Never expires them first — a purchase is not a
    grant, so `grant_credits`' no-rollover reset does not apply to it."""
    if amount < 1:
        raise ValueError("A purchase must add at least one credit.")
    _lock_workspace(workspace)
    return _append(CreditLedger, workspace, delta=amount, reason=CreditReason.PURCHASE, note=note)


@transaction.atomic
def adjust_credits(workspace: Workspace, delta: int, *, actor: Any, note: str) -> CreditLedger:
    """An operator correction. Attributed on purpose — an unattributed
    adjustment is indistinguishable from a bug in the debit path."""
    if not note:
        raise ValueError("A manual adjustment must carry a note.")
    _lock_workspace(workspace)
    return _append(
        CreditLedger,
        workspace,
        delta=delta,
        reason=CreditReason.MANUAL_ADJUST,
        actor=actor,
        note=note,
    )


# -----------------------------------------------------------------------------
# Video units
# -----------------------------------------------------------------------------
@transaction.atomic
def debit_video_units(
    workspace: Workspace,
    units: int,
    *,
    quota: int,
    unit_cost_cents: int = 0,
    note: str = "",
    generation: Any = None,
) -> VideoLedger:
    if units < 0:
        raise ValueError("Use refund_video_units() to return units.")

    _lock_workspace(workspace)
    balance = _balance(VideoLedger, workspace)

    unlimited = quota == UNLIMITED
    if not unlimited and balance < units:
        raise InsufficientVideoUnits(
            f"This needs {units} video unit(s); {balance} remaining.",
            detail={"required": units, "available": balance},
        )

    return _append(
        VideoLedger,
        workspace,
        delta=0 if unlimited else -units,
        reason=VideoReason.GENERATION,
        unit_cost_cents=unit_cost_cents,
        note=note,
        generation=generation,
    )


@transaction.atomic
def refund_video_units(entry: VideoLedger, *, note: str = "") -> VideoLedger:
    existing = VideoLedger.objects.filter(reverses=entry, reason=VideoReason.REFUND).first()
    if existing is not None:
        return existing

    _lock_workspace(entry.workspace)
    return _append(
        VideoLedger,
        entry.workspace,
        delta=-entry.delta,
        reason=VideoReason.REFUND,
        reverses=entry,
        unit_cost_cents=entry.unit_cost_cents,
        generation=entry.generation,
        note=note or "generation failed",
    )


@transaction.atomic
def grant_video_units(
    workspace: Workspace,
    amount: int,
    *,
    reason: str = VideoReason.MONTHLY_GRANT,
    note: str = "",
) -> list[VideoLedger]:
    """Included video allowance resets monthly and does not roll over (§4.3).

    Prepaid packs are bought separately and *do* persist, so the reset zeroes
    only what the monthly grant put there — anything bought stays.
    """
    _lock_workspace(workspace)
    rows: list[VideoLedger] = []

    granted = _granted_video_units(workspace)
    if granted > 0:
        rows.append(
            _append(
                VideoLedger,
                workspace,
                delta=-granted,
                reason=reason,
                note="expired: included videos do not roll over",
            )
        )

    rows.append(_append(VideoLedger, workspace, delta=amount, reason=reason, note=note))
    return rows


@transaction.atomic
def purchase_video_units(
    workspace: Workspace, amount: int, *, unit_cost_cents: int = 0, note: str
) -> VideoLedger:
    """Adds prepaid video units (§4.3, D16).

    Written with `reason=PURCHASE` on purpose: the monthly reset only takes back
    what `MONTHLY_GRANT` put there, so bought units survive the rollover — which
    is the whole difference between an allowance and a pack.
    """
    if amount < 1:
        raise ValueError("A purchase must add at least one video unit.")
    _lock_workspace(workspace)
    return _append(
        VideoLedger,
        workspace,
        delta=amount,
        reason=VideoReason.PURCHASE,
        unit_cost_cents=unit_cost_cents,
        note=note,
    )


def _granted_video_units(workspace: Workspace) -> int:
    """The part of the balance that came from the monthly allowance rather than
    a purchase — the only part the monthly reset is allowed to take back."""
    total = VideoLedger.objects.filter(
        workspace=workspace, reason=VideoReason.MONTHLY_GRANT
    ).aggregate(total=Sum("delta"))["total"]
    return max(0, int(total or 0))


# -----------------------------------------------------------------------------
# Reconciliation
# -----------------------------------------------------------------------------
def find_drift[LedgerT: (CreditLedger, VideoLedger)](
    model: type[LedgerT], workspace: Workspace
) -> list[dict[str, Any]]:
    """Rows whose `balance_after` disagrees with the running `SUM(delta)`.

    Drift means the debit path wrote a row outside the lock, or something
    bypassed this module. It is never benign.
    """
    running = 0
    drifted: list[dict[str, Any]] = []
    entries: list[LedgerT] = list(
        model.objects.filter(workspace=workspace).order_by("created_at", "id")
    )

    for entry in entries:
        running += entry.delta
        if entry.balance_after != running:
            drifted.append(
                {
                    "entry_id": entry.pk,
                    "recorded": entry.balance_after,
                    "expected": running,
                }
            )
    return drifted
