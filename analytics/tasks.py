"""Analytics tasks (design.md §5.1: `metrics_q` for capture, `trends_q` for the
nightly repurpose scan).

Thin wrappers, like every other task body in the project — the bodies are one
call each so the logic stays unit-testable without a broker.
"""

from __future__ import annotations

from celery import shared_task

from analytics.services import ingest, repurposing


@shared_task(name="analytics.tasks.capture_due_metrics")
def capture_due_metrics() -> int:
    return ingest.capture_due()


@shared_task(name="analytics.tasks.snapshot_accounts")
def snapshot_accounts() -> int:
    return ingest.snapshot_accounts()


@shared_task(name="analytics.tasks.scan_repurpose_candidates")
def scan_repurpose_candidates() -> int:
    return repurposing.scan_all()
