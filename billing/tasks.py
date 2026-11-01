"""Billing Beat tasks.

Thin wrappers over `billing.services` (implementation.md §4.1, A4): each body is
one call, so the logic is unit-testable without a broker.
"""

from __future__ import annotations

import logging

from celery import shared_task

from billing.services import subscriptions
from billing.services.reconciliation import reconcile_all
from billing.services.reconciliation import reconcile_organizations as _reconcile_organizations

logger = logging.getLogger(__name__)


@shared_task(name="billing.tasks.grant_monthly_allowances")
def grant_monthly_allowances() -> int:
    return subscriptions.grant_due_period_allowances()


@shared_task(name="billing.tasks.expire_trials")
def expire_trials() -> int:
    return subscriptions.expire_lapsed_trials()


@shared_task(name="billing.tasks.reconcile_ledgers")
def reconcile_ledgers() -> int:
    return reconcile_all()


@shared_task(name="billing.tasks.reconcile_organizations")
def reconcile_organizations() -> int:
    """The org-dimension nightly sweep (P0-53, P0-62).

    Ledger scope, plan parity and subscription quantity, all of which
    **report and never repair** — a repair would hide the write path that
    caused the drift, and that path is the actual bug.
    """
    return _reconcile_organizations()


@shared_task(name="billing.tasks.expire_addon_trials")
def expire_addon_trials() -> int:
    """The 02:45 add-on trial sweep (P0-19)."""
    from workspaces.services.addons import expire_addon_trials as sweep

    return sweep()


# TND prepaid periods (`payments-tunisia.md` §5) --------------------------------
@shared_task(name="billing.tasks.reconcile_prepaid")
def reconcile_prepaid() -> int:
    """Every 10 minutes: re-read unconfirmed Konnect payments (a lost notification)."""
    from billing.services.prepaid import reread_pending

    return reread_pending()


@shared_task(name="billing.tasks.prepaid_lifecycle")
def prepaid_lifecycle() -> dict[str, int]:
    """Daily: renewal reminders, grace, then the default plan."""
    from billing.services.prepaid import run_lifecycle

    return run_lifecycle()


@shared_task(name="billing.tasks.audit_prepaid")
def audit_prepaid() -> int:
    """Nightly: paid orders read back again. Reports, never repairs."""
    from billing.services.prepaid import audit_paid

    return len(audit_paid())


@shared_task(name="billing.tasks.issue_invoices")
def issue_invoices() -> int:
    """Daily: last month's TND invoices, and any month a failed run missed."""
    from billing.services.invoicing import issue_due

    return issue_due()
