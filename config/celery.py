"""Celery application and queue topology (design.md §5.1).

Publishing has a hard deadline — a post scheduled for 09:00 goes at 09:00 —
while trend extraction and metrics polling are elastic. Those profiles must
never share a worker pool, so each queue is declared separately here and run as
its own worker deployment (implementation.md Phase 16.5).

| queue      | priority          | failure behaviour                          |
|------------|-------------------|--------------------------------------------|
| publish_q  | highest, dedicated| back off on 429/5xx; alert on 3rd failure  |
| remind_q   | high              | retry; reminders are time-critical         |
| media_q    | high              | fail fast, refund, surface to user         |
| ai_q       | high              | fail fast, refund, surface to user         |
| metrics_q  | low               | silent retry, tolerate gaps                |
| trends_q   | lowest            | silent retry, last corpus stays valid      |
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("occs")
app.config_from_object("django.conf:settings", namespace="CELERY")

QUEUE_NAMES = (
    "publish_q",
    "remind_q",
    "media_q",
    "ai_q",
    "metrics_q",
    "trends_q",
)

app.conf.task_queues = tuple(Queue(name) for name in QUEUE_NAMES)

# Nothing runs on the implicit "celery" queue: an unrouted task is a routing bug
# and should be visible, not quietly absorbed by a default pool.
app.conf.task_default_queue = "default"

# Routed by task name. Keeping this as data rather than per-task decorators
# means the whole topology is reviewable in one place.
app.conf.task_routes = {
    "scheduling.tasks.publish*": {"queue": "publish_q"},
    "content.tasks.publish*": {"queue": "publish_q"},
    "reminders.tasks.*": {"queue": "remind_q"},
    # Transactional auth mail shares the reminder pool: same profile —
    # high priority, retried, and someone is waiting on it.
    "accounts.tasks.*": {"queue": "remind_q"},
    "scheduling.tasks.remind*": {"queue": "remind_q"},
    "content.tasks.media*": {"queue": "media_q"},
    "ai.tasks.video*": {"queue": "media_q"},
    "ai.tasks.*": {"queue": "ai_q"},
    "analytics.tasks.*": {"queue": "metrics_q"},
    "trends.tasks.*": {"queue": "trends_q"},
    # Grants, trial expiry and reconciliation are periodic bookkeeping: nobody
    # is waiting on them, and they must never delay a scheduled publish.
    "billing.tasks.*": {"queue": "metrics_q"},
}

app.autodiscover_tasks()


@app.task(bind=True, name="config.debug_task")
def debug_task(self: Any) -> str:
    return f"request: {self.request!r}"
