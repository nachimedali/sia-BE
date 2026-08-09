"""Thin Celery wrapper over `ai.services.pipeline` (implementation.md §4.1, A4).

Routed to `ai_q` by `config/celery.py`'s `"ai.tasks.*"` rule — no provider
call happens inline in a request/response cycle (design.md §11); the view
creates the `PENDING` row synchronously and this task does the rest.
"""

from __future__ import annotations

from celery import shared_task

from ai.models import Generation
from ai.services.pipeline import run_generation


@shared_task(name="ai.tasks.run_generation")
def run_generation_task(generation_id: int, *, n: int = 3) -> None:
    generation = Generation.objects.get(pk=generation_id)
    run_generation(generation, n=n)
