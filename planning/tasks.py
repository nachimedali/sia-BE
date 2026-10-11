"""Planning tasks.

**One task per bulk item** (P3-12), explicitly routed like every other task
here — nothing runs on an implicit default queue (Part 7 rule 9).
"""

from __future__ import annotations

from celery import shared_task

from planning.services import bulk


@shared_task(name="planning.tasks.run_bulk_item")
def run_bulk_item(item_id: int) -> None:
    """Runs one item of one operation.

    Deliberately **no retry policy**. `run_item` records failure as data rather
    than raising, so there is nothing for Celery to retry: a retry here would
    either repeat work that already settled or re-run a failure whose reason is
    already written down and visible to the customer.
    """
    bulk.run_item(item_id)


@shared_task(name="planning.tasks.run_campaign_plan")
def run_campaign_plan(plan_id: int) -> None:
    """Writes one campaign plan (steps-plan S5). On `ai_q`, beside every other
    provider call. No retry policy: `run_plan` records failure on the row, and
    a retried task finds the plan no longer `QUEUED` and stops."""
    from planning.services.plans import run_plan

    run_plan(plan_id)
