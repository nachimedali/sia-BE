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


@shared_task(
    name="accounts.tasks.build_data_export", bind=True, max_retries=2, default_retry_delay=60
)
def build_data_export(self: Any, export_id: int) -> None:
    """Builds one person's data export and emails them (P2). Routed to
    `media_q` (config/celery.py): it writes a file, and nobody is blocked on it
    the way they are on a verification link."""
    from accounts.models import DataExport, DataExportState
    from accounts.services.self_service import build_export

    export = DataExport.objects.select_related("user").filter(pk=export_id).first()
    if export is None or export.state != DataExportState.PREPARING:
        return
    try:
        build_export(export)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            DataExport.objects.filter(pk=export_id).update(state=DataExportState.FAILED)
            logger.exception("data export failed", extra={"export_id": export_id})
            return
        raise self.retry(exc=exc) from exc
