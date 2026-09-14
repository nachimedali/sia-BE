"""Analytics tasks (design.md §5.1: `metrics_q` for capture, `trends_q` for the
nightly repurpose scan).

Thin wrappers, like every other task body in the project — the bodies are one
call each so the logic stays unit-testable without a broker.
"""

from __future__ import annotations

from celery import shared_task

from analytics.services import ingest, repurposing, schedules


@shared_task(name="analytics.tasks.capture_due_metrics")
def capture_due_metrics() -> int:
    return ingest.capture_due()


@shared_task(name="analytics.tasks.follow_metrics_delta")
def follow_metrics_delta() -> int:
    """The cheap path (P0-31): one feed read for every account that moved."""
    return ingest.follow_delta()


@shared_task(name="analytics.tasks.snapshot_accounts")
def snapshot_accounts() -> int:
    return ingest.snapshot_accounts()


@shared_task(name="analytics.tasks.scan_repurpose_candidates")
def scan_repurpose_candidates() -> int:
    return repurposing.scan_all()


@shared_task(name="analytics.tasks.run_due_reports")
def run_due_reports() -> int:
    """Monthly reports, in each workspace's own zone (P6-07).

    Hourly rather than monthly, because "the first of the month" happens
    fourteen times across the zones we serve — one monthly tick would be the
    wrong instant for nearly everybody. `is_due` is what stops that becoming
    24 documents: a report is owed only while no run covers its window.
    """
    return schedules.run_due()
