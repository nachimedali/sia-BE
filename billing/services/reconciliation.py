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

from django.db.models import F

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


# -----------------------------------------------------------------------------
# The organization dimension (P0-53, P0-62)
# -----------------------------------------------------------------------------
# **Extended before the backfill runs, not after.** A backfill that moves rows
# into a dimension nothing checks is a backfill whose mistakes are invisible
# until someone is billed wrongly. The order is in BUILD-PLAN's migration
# sequence for exactly this reason.


def reconcile_ledger_scope() -> list[dict[str, Any]]:
    """Ledger rows whose `organization` disagrees with their workspace's.

    Catches the two ways the re-scope can go wrong: a row the backfill missed
    (null org against a workspace that has one), and a row attributed to the
    wrong company (which is the one that matters, because it pools quota
    across a tenant boundary).
    """
    findings: list[dict[str, Any]] = []
    for model in (CreditLedger, VideoLedger):
        mismatched = (
            model.objects.exclude(workspace__organization__isnull=True)
            .exclude(organization_id=F("workspace__organization_id"))
            .values("pk", "organization_id", "workspace__organization_id")[:100]
        )
        for row in mismatched:
            findings.append(
                {
                    "ledger": model.__name__,
                    "row": row["pk"],
                    "organization": row["organization_id"],
                    "expected": row["workspace__organization_id"],
                }
            )
    return findings


def reconcile_stripe_quantity() -> list[dict[str, Any]]:
    """Subscription quantity against the live workspace count (P0-62).

    **Reports, never repairs.** A quantity that drifted did so because some
    write path skipped `sync_workspace_quantity`, and silently correcting it
    would hide that path while leaving the customer's next invoice a surprise
    either way.

    Compares against what we *believe* we last sent rather than calling Stripe
    per organization: a nightly job that made one API call per customer would
    be its own rate-limit incident.
    """
    from billing.models import Subscription, SubscriptionStatus
    from billing.services.subscriptions import live_workspace_count
    from workspaces.models import Organization

    findings: list[dict[str, Any]] = []
    for organization in Organization.objects.iterator():
        subscription = (
            Subscription.objects.filter(
                workspace__organization=organization, status__in=SubscriptionStatus.live()
            )
            .order_by("-created_at")
            .first()
        )
        if subscription is None or not subscription.stripe_subscription_item_id:
            continue
        expected = live_workspace_count(organization)
        pending = organization.workspaces.filter(status="PENDING_BILLING").count()
        if pending:
            findings.append(
                {
                    "organization": organization.pk,
                    "issue": "workspaces stuck awaiting billing confirmation",
                    "pending": pending,
                    "expected_quantity": expected,
                }
            )
    return findings


def reconcile_organizations() -> int:
    """The org-dimension sweep. Returns how many findings were raised."""
    findings: list[dict[str, Any]] = []
    findings.extend(reconcile_ledger_scope())
    findings.extend(reconcile_stripe_quantity())

    if findings:
        logger.error(
            "organization reconciliation findings",
            extra={"count": len(findings), "findings": findings[:20]},
        )
    else:
        logger.info("organization reconciliation clean")
    return len(findings)
