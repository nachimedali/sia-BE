"""Reminder Beat tasks (design.md §5.1: `remind_q`, high priority, retried).

Thin wrappers over `reminders.services` (implementation.md §4.1) — each body
is one call, so it is unit-testable without a broker.
"""

from __future__ import annotations

from celery import shared_task

from reminders import services


@shared_task(name="reminders.tasks.send_due_reminders")
def send_due_reminders() -> int:
    return services.send_due()


@shared_task(name="reminders.tasks.expire_stale_reminders")
def expire_stale_reminders() -> int:
    return services.expire_stale()
