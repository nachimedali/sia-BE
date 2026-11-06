"""Thin Celery wrapper over `ai.services.pipeline` (implementation.md §4.1, A4).

Routed to `ai_q` by `config/celery.py`'s `"ai.tasks.*"` rule — no provider
call happens inline in a request/response cycle (design.md §11); the view
creates the `PENDING` row synchronously and this task does the rest.
"""

from __future__ import annotations

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from ai.models import Generation, GenerationStatus
from ai.services.pipeline import run_generation


@shared_task(
    name="ai.tasks.run_generation",
    soft_time_limit=settings.GENERATION_TASK_TIMEOUT_SECONDS,
    time_limit=settings.GENERATION_TASK_TIMEOUT_SECONDS + 30,
)
def run_generation_task(generation_id: int, *, n: int = 3) -> None:
    generation = Generation.objects.get(pk=generation_id)
    try:
        run_generation(generation, n=n)
    except SoftTimeLimitExceeded:
        # A hung provider call must end as a visible failure, not a row that
        # stays PENDING forever. Credits are debited on pass only, so nothing
        # is owed back.
        Generation.objects.filter(pk=generation_id, status=GenerationStatus.PENDING).update(
            status=GenerationStatus.FAILED, error_detail={"reason": "timeout"}
        )


@shared_task(name="ai.tasks.video_render")
def run_video_render(render_id: int) -> None:
    """Routed to `media_q` by the `"ai.tasks.video*"` rule: a render is long,
    file-heavy work, kept off the queue the Studio's image runs wait on."""
    from ai.services.video import run_render

    run_render(render_id)
