"""Playbook collection (`trends_q`: lowest priority, the last wall stays valid).

A thin wrapper. Beat calls it every half hour; each source decides whether it
is due (`PlaybookSource.refresh_minutes`), so the schedule never has to know a
provider's quota.
"""

from __future__ import annotations

from celery import shared_task

from playbook.services import collect


@shared_task(name="playbook.tasks.collect_playbook")
def collect_playbook() -> dict[str, int]:
    return collect.collect_due()
