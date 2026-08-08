"""Billing Beat tasks.

Thin wrappers over `billing.services` (implementation.md §4.1, A4): each body is
one call, so the logic is unit-testable without a broker.
"""

from __future__ import annotations

import logging

from celery import shared_task

from billing.services import subscriptions
from billing.services.reconciliation import reconcile_all

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
