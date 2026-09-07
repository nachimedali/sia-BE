"""Content tasks.

Thin wrappers, like every other task body in the project — the bodies are one
call each so the logic stays unit-testable without a broker.

Routing is declared as data in `config/celery.py` (Part 7 rule 9). These land
on `metrics_q`, the pool the billing sweeps already share: periodic
bookkeeping that nobody is waiting on and that must never delay a scheduled
publish.
"""

from __future__ import annotations

from celery import shared_task

from content.services import recurrence, revisions


@shared_task(name="content.tasks.prune_post_revisions")
def prune_post_revisions() -> int:
    return revisions.prune_expired()


@shared_task(name="content.tasks.recurrence_materialise")
def recurrence_materialise() -> int:
    return recurrence.materialise_due()
