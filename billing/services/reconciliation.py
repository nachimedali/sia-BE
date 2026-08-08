"""Nightly ledger reconciliation (design.md §8.2, implementation.md Phase 3.8).

Asserts that every row's `balance_after` equals the running `SUM(delta)` before
it. Drift means the debit path wrote outside the row lock, or something wrote a
ledger row without going through `billing.services.ledger`.

**It reports; it never repairs.** Silently correcting `balance_after` would hide
the bug that caused the drift, and the drift is the only symptom. Billing bugs
that fix their own evidence are how revenue leaks quietly.
"""

from __future__ import annotations

import logging
from typing import Any

from billing.models import CreditLedger, VideoLedger
from billing.services.ledger import find_drift
from workspaces.models import Workspace

logger = logging.getLogger(__name__)


def reconcile_workspace(workspace: Workspace) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    # Written out rather than looped: `find_drift` is generic over the two
    # concrete ledgers, and a loop variable erases that back to the base class.
    for drift in find_drift(CreditLedger, workspace):
        findings.append({"ledger": "CreditLedger", "workspace_id": workspace.pk, **drift})
    for drift in find_drift(VideoLedger, workspace):
        findings.append({"ledger": "VideoLedger", "workspace_id": workspace.pk, **drift})
    return findings


def reconcile_all() -> int:
    """Returns the number of drifted rows found. Non-zero should page someone."""
    findings: list[dict[str, Any]] = []
    for workspace in Workspace.objects.iterator():
        findings.extend(reconcile_workspace(workspace))

    if findings:
        logger.error(
            "ledger drift detected",
            extra={"drift_count": len(findings), "findings": findings[:20]},
        )
    else:
        logger.info("ledger reconciliation clean")
    return len(findings)
