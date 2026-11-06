"""Queue smoke tasks.

`ping` exists so every queue in design.md §5.1 can be shown to accept and drain
work — the Phase 1 completion gate — without any real subsystem being built yet.
"""

from __future__ import annotations

import time
from typing import Any

from celery import shared_task

from common.redis import get_redis

BEAT_HEARTBEAT_KEY = "beat:heartbeat"


@shared_task(name="common.tasks.ping")
def ping(queue: str = "default") -> dict[str, Any]:
    return {"pong": True, "queue": queue}


@shared_task(name="common.tasks.beat_heartbeat")
def beat_heartbeat() -> None:
    """Written every minute by Beat; `/health/beat/` reads it, so a stopped Beat
    (or a stopped publish worker, which runs it) is noticed by an uptime check."""
    get_redis().set(BEAT_HEARTBEAT_KEY, int(time.time()))
