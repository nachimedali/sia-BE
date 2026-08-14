"""Autopilot tasks (design.md §5.1).

Routed to `ai_q`, not to a bookkeeping queue: the body of this task is
generation, so it belongs in the pool sized for provider latency
(`--concurrency=12`) rather than one sized for database work.

A thin wrapper, like every other task body in the project — the scan itself is
`products.services.autopilot.run_due`, which is what the tests call.
"""

from __future__ import annotations

from celery import shared_task

from products.services import autopilot


@shared_task(name="products.tasks.run_due_autopilot")
def run_due_autopilot() -> int:
    return autopilot.run_due()
