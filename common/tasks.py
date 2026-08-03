"""Queue smoke tasks.

`ping` exists so every queue in design.md §5.1 can be shown to accept and drain
work — the Phase 1 completion gate — without any real subsystem being built yet.
"""

from __future__ import annotations

from typing import Any

from celery import shared_task


@shared_task(name="common.tasks.ping")
def ping(queue: str = "default") -> dict[str, Any]:
    return {"pong": True, "queue": queue}
