"""Notification delivery (P2-12).

Routed to `notify_q` (`config/celery.py`): high priority with retry, because
somebody is waiting to be told — but a separate pool from `publish_q`, because
a slow SMTP server must never be able to delay a scheduled post.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="notifications.tasks.deliver_notification",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def deliver_notification(
    self: Any,
    *,
    user_id: int,
    workspace_id: int,
    event_key: str,
    payload: dict[str, Any],
    transports: list[str],
) -> int:
    """Deliver one event to one person over each chosen transport.

    Ids rather than model instances in the signature: a task argument is
    serialised onto a broker, and a pickled model is a stale copy of a row that
    may have changed by the time a worker picks it up.

    **One failing transport does not lose the others.** An SMTP outage must not
    take the in-app record with it — that record is the evidence the person was
    told, and it is the cheapest of the three to write.
    """
    from django.contrib.auth import get_user_model

    from notifications.transports import registry
    from workspaces.models import Workspace

    user = get_user_model().objects.filter(pk=user_id).first()
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if user is None or workspace is None:
        # Deleted between queueing and delivery. Not an error worth retrying:
        # there is nobody to tell.
        return 0

    available = registry()
    delivered = 0
    failures: list[Exception] = []
    for key in transports:
        transport = available.get(key)
        if transport is None:
            logger.warning("unknown notification transport", extra={"transport": key})
            continue
        try:
            transport.deliver(user=user, workspace=workspace, event_key=event_key, payload=payload)
        except Exception as exc:
            logger.warning(
                "notification transport failed",
                extra={"transport": key, "event_key": event_key},
            )
            failures.append(exc)
        else:
            delivered += 1

    if failures and delivered == 0:
        # Everything failed, so this is a transient problem rather than one bad
        # transport — worth another attempt.
        raise self.retry(exc=failures[0])
    return delivered
