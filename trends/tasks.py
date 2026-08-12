"""Trend tasks (design.md §5.1: `trends_q`, lowest priority, concurrency 2).

A thin wrapper, like every other task body in the project. Extraction is on
demand (D11) rather than scheduled, so this exists for the caller that does not
want to wait — a refresh pressed in the UI hands off here and the screen keeps
serving the cached corpus until the new one lands.
"""

from __future__ import annotations

from celery import shared_task

from trends import services


@shared_task(name="trends.tasks.extract_trends")
def extract_trends(category_id: int, platform: str, *, force: bool = False) -> int:
    return len(services.extract(category_id=category_id, platform=platform, force=force))
