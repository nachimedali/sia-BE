"""Routed to `ai_q` by `config/celery.py`'s `"checks.tasks.*"` rule: the checks
call the OCR and policy readers, which are provider latency."""

from __future__ import annotations

from celery import shared_task


@shared_task(name="checks.tasks.run_post_checks")
def run_post_checks(post_id: int, actor_id: int | None = None, run_id: int | None = None) -> None:
    from accounts.models import User
    from checks.models import CheckRun
    from checks.services import PREFETCH, run_checks
    from content.models import Post

    post = (
        Post.objects.filter(pk=post_id)
        .select_related("workspace", "category")
        .prefetch_related(*PREFETCH)
        .first()
    )
    if post is None:
        return
    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    run = CheckRun.objects.filter(pk=run_id).first() if run_id else None
    run_checks(post, actor=actor, run=run)
