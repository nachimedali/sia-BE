"""Publish Beat tasks (design.md §5.1: `publish_q`, highest priority, dedicated).

Thin wrappers over `scheduling.publishing` (implementation.md §4.1) — the
bodies are one call each, so the logic is unit-testable without a broker. The
one thing that cannot live in the service is the back-off itself: only the
task knows how many attempts it has already spent, so the retry decision is
made here and the service just says whether another attempt is worth making
(`PlatformError.retryable`).
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from channels.adapters.base import PlatformError
from scheduling import publishing

#: Doubling from one minute, capped so a long provider outage still gets a
#: retry every ten minutes rather than drifting into hours.
RETRY_BACKOFF_SECONDS = 60
RETRY_BACKOFF_MAX_SECONDS = 600


@shared_task(name="scheduling.tasks.publish_due_posts")
def publish_due_posts() -> int:
    """The scan. Dispatches one task per post rather than publishing inline:
    a slow provider on one workspace's post must not delay the next
    workspace's 09:00 slot."""
    post_ids = publishing.due_post_ids()
    for post_id in post_ids:
        publish_post.delay(post_id)
    return len(post_ids)


@shared_task(
    bind=True,
    name="scheduling.tasks.publish_post",
    max_retries=publishing.MAX_ATTEMPTS - 1,
)
def publish_post(self: Any, post_id: int) -> None:
    # `publish_post_by_id` raises only when another attempt would help: a
    # terminal failure is recorded, alerted on and swallowed there, because
    # the post it belongs to may still have other targets that succeeded.
    # So everything that reaches here is by definition worth retrying, and
    # there is no non-retryable branch to write —
    # `test_a_terminal_failure_is_not_retried_at_all` is what holds that
    # contract in place.
    try:
        publishing.publish_post_by_id(post_id)
    except PlatformError as error:
        countdown = _countdown(int(self.request.retries), error)
        raise self.retry(exc=error, countdown=countdown) from error


def _countdown(retries: int, error: PlatformError) -> float:
    """Honours the provider's own `Retry-After` when it sent one — it knows
    when the window reopens and we are only guessing."""
    if error.retry_after:
        return float(error.retry_after)
    # `float(...)` because mypy widens `int ** int` to Any (a negative
    # exponent would make it a float), and this function promises a float.
    return float(min(RETRY_BACKOFF_SECONDS * 2**retries, RETRY_BACKOFF_MAX_SECONDS))
