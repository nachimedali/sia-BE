"""Provider housekeeping, and processing of received webhooks (SA-40/42).

Webhook processing is split by what it decides: post and account lifecycle on
`publish_q` (it decides whether posts go out), the audience on `metrics_q`.
Both are routed explicitly in `config/celery.py`.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from social_providers.models import DeliveryStatus, ProviderCall, WebhookDelivery


@shared_task(name="social_providers.tasks.prune_provider_calls")
def prune_provider_calls() -> int:
    """Delete call-log rows past the cockpit's window. Operational data, not
    an audit trail — the audit trail is `AuditLog` and the posts themselves."""
    cutoff = timezone.now() - dt.timedelta(days=int(settings.SOCIALAPI_CALL_RETENTION_DAYS))
    deleted, _ = ProviderCall.objects.filter(created_at__lt=cutoff).delete()
    return deleted


logger = logging.getLogger(__name__)

#: A delivery left `RECEIVED` or `PROCESSING` this long (the queue was down, a
#: worker died mid-way) is handed out again by the sweep.
STALE_AFTER = dt.timedelta(minutes=10)
#: Attempts before a delivery stays `FAILED` for a person to look at.
MAX_ATTEMPTS = 5


def enqueue(delivery: WebhookDelivery) -> None:
    """Hands one recorded delivery to its queue."""
    from social_providers.socialapi.events import LIFECYCLE

    task = process_webhook_lifecycle if delivery.event in LIFECYCLE else process_webhook_inbox
    task.delay(delivery.pk)


def process(delivery_pk: int) -> str:
    """Claims and processes one delivery. Returns its final status.

    Failure is recorded and re-raised, so the task's own retry runs it again;
    the vendor is never asked to resend, because it was already told yes.
    """
    from social_providers.socialapi import webhooks
    from social_providers.socialapi.events import route

    delivery = webhooks.claim(delivery_pk)
    if delivery is None:
        return ""
    delivery.attempts += 1
    try:
        status, note = route(delivery.event, delivery.payload)
    except Exception as exc:
        delivery.status = DeliveryStatus.FAILED
        delivery.note = f"{type(exc).__name__}: {exc}"[:280]
        delivery.save(update_fields=["status", "note", "attempts"])
        logger.warning(
            "webhook processing failed",
            exc_info=True,
            extra={"delivery": delivery.delivery_id, "event": delivery.event},
        )
        raise
    delivery.status = status
    delivery.note = note[:280]
    delivery.processed_at = timezone.now()
    fields = ["status", "note", "attempts", "processed_at"]
    if status == DeliveryStatus.FOREIGN:
        delivery.payload = {}  # never keep another tenant's event
        fields.append("payload")
    delivery.save(update_fields=fields)
    return str(status)


@shared_task(
    name="social_providers.tasks.process_webhook_lifecycle",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=MAX_ATTEMPTS - 1,
)
def process_webhook_lifecycle(delivery_pk: int) -> str:
    return process(delivery_pk)


@shared_task(
    name="social_providers.tasks.process_webhook_inbox",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=MAX_ATTEMPTS - 1,
)
def process_webhook_inbox(delivery_pk: int) -> str:
    return process(delivery_pk)


@shared_task(name="social_providers.tasks.sweep_webhook_deliveries")
def sweep_webhook_deliveries() -> int:
    """Re-queues deliveries the queue never ran (it was down when they
    arrived) or a worker abandoned, and prunes old ones. `KEPT` deliveries
    stay until their consumer takes them (mentions, S2)."""
    now = timezone.now()
    stuck = WebhookDelivery.objects.filter(
        Q(status__in=(DeliveryStatus.RECEIVED, DeliveryStatus.PROCESSING))
        | Q(status=DeliveryStatus.FAILED, attempts__lt=MAX_ATTEMPTS),
        received_at__lte=now - STALE_AFTER,
    )
    requeued = 0
    for delivery in stuck[:200]:
        if delivery.status == DeliveryStatus.PROCESSING:
            WebhookDelivery.objects.filter(pk=delivery.pk, status=DeliveryStatus.PROCESSING).update(
                status=DeliveryStatus.RECEIVED
            )
        enqueue(delivery)
        requeued += 1
    cutoff = now - dt.timedelta(days=int(settings.SOCIALAPI_WEBHOOK_RETENTION_DAYS))
    WebhookDelivery.objects.filter(received_at__lt=cutoff).exclude(
        status=DeliveryStatus.KEPT
    ).delete()
    return requeued
