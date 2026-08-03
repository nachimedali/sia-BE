"""Auth email delivery.

Routed to `remind_q` (config/celery.py): it is the transactional-email pool —
high priority, retried, time-critical — which is exactly the profile of a
verification link a user is sitting and waiting for.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from common.mail import Email, get_mail_sender

logger = logging.getLogger(__name__)


@shared_task(
    name="accounts.tasks.send_templated_email",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def send_templated_email(
    self: Any, to: str, subject: str, template: str, context: dict[str, Any]
) -> None:
    try:
        get_mail_sender().send(Email(to=to, subject=subject, template=template, context=context))
    except Exception as exc:
        # A transient SMTP failure must not silently lose a verification link.
        logger.warning("email send failed, retrying", extra={"template": template})
        raise self.retry(exc=exc) from exc
