"""Receiving SocialAPI's webhooks (SA-40, plan §7 and G10).

Order of operations, each step before the next:

1. **No delivery id → answer `204` and do nothing.** The vendor's verification
   ping (`webhook.test`) and its test deliveries carry none, and the ping is
   signed with a secret we cannot know yet (it is revealed only after the ping
   succeeds). Nothing is ever processed from such a request, so answering it
   unverified opens nothing.
2. **Verify before parsing**: the V2 signature (HMAC-SHA256 of
   `<timestamp>.<raw body>`), compared in constant time, against the current
   secret and, during a rotation, the previous one; and the signed timestamp
   within the tolerance (five minutes), which bounds a replay of a captured
   request.
3. **Record, deduplicated on the delivery id** (stable across the vendor's
   retries), then answer. The vendor allows ten seconds and retries anything
   else; the work runs in a task (`social_providers.tasks`), so a slow handler
   never turns into a duplicate delivery.

A refused request is counted for the cockpit, never stored: an unverified body
is untrusted input, and keeping it would be an open write path.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from social_providers.models import DeliveryStatus, WebhookDelivery

logger = logging.getLogger(__name__)

PROVIDER = "socialapi"

#: Counters the cockpit reads, per day (SA-80).
COUNTERS = ("refused_signature", "refused_stale", "duplicate", "unverifiable_ping")


class WebhookRefusedError(Exception):
    """The request is not a delivery we can trust. Carries the error code and
    HTTP status the receiver answers with."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Received:
    delivery: WebhookDelivery | None
    #: `False` for a ping, a test delivery or a duplicate: nothing to queue.
    fresh: bool


def _secrets() -> list[str]:
    return [
        secret
        for secret in (
            str(getattr(settings, "SOCIALAPI_WEBHOOK_SECRET", "") or ""),
            str(getattr(settings, "SOCIALAPI_WEBHOOK_SECRET_PREVIOUS", "") or ""),
        )
        if secret
    ]


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The V2 signature the vendor sends: `sha256=` + hex HMAC of
    `<timestamp>.<raw body>`."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def count(name: str) -> None:
    """One more of `name` today. Best-effort: a cache outage never fails a
    delivery."""
    key = f"socialapi:webhooks:{timezone.now():%Y-%m-%d}:{name}"
    try:
        cache.add(key, 0, timeout=8 * 24 * 3600)
        cache.incr(key)
    except Exception:
        logger.debug("webhook counter unavailable", exc_info=True)


def counts(day: dt.date) -> dict[str, int]:
    """The day's counters, zero where nothing was counted."""
    found = cache.get_many([f"socialapi:webhooks:{day:%Y-%m-%d}:{name}" for name in COUNTERS])
    return {
        name: int(found.get(f"socialapi:webhooks:{day:%Y-%m-%d}:{name}") or 0) for name in COUNTERS
    }


def verify(body: bytes, headers: Any, *, now: dt.datetime | None = None) -> None:
    """Raises `WebhookRefusedError` unless the body was signed by the vendor with
    one of our secrets, recently."""
    secrets = _secrets()
    if not secrets:
        # Answered with a retryable status: the vendor keeps retrying for
        # hours, and a secret put in place meanwhile lets the retry through.
        raise WebhookRefusedError(
            "Webhooks are not configured: SOCIALAPI_WEBHOOK_SECRET is not set.",
            code="webhook_not_configured",
            status=503,
        )
    timestamp = str(headers.get("X-SocialAPI-Timestamp") or "")
    sent = str(headers.get("X-SocialAPI-Signature-V2") or "")
    try:
        sent_at = int(timestamp)
    except ValueError:
        count("refused_signature")
        raise WebhookRefusedError(
            "The delivery carries no valid timestamp.", code="invalid_signature", status=400
        ) from None
    if not any(hmac.compare_digest(signature(s, timestamp, body), sent) for s in secrets):
        count("refused_signature")
        raise WebhookRefusedError(
            "Webhook signature verification failed.", code="invalid_signature", status=400
        )
    moment = (now or timezone.now()).timestamp()
    if abs(moment - sent_at) > int(settings.SOCIALAPI_WEBHOOK_TOLERANCE_SECONDS):
        count("refused_stale")
        raise WebhookRefusedError(
            "The delivery is too old to be trusted.", code="stale_delivery", status=400
        )


def receive(body: bytes, headers: Any, *, now: dt.datetime | None = None) -> Received:
    """Verifies and records one request. Never processes it."""
    delivery_id = str(headers.get("X-SocialAPI-Delivery") or "").strip()
    if not delivery_id:
        count("unverifiable_ping")
        return Received(delivery=None, fresh=False)

    verify(body, headers, now=now)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        raise WebhookRefusedError(
            "The delivery is not a webhook event.", code="invalid_payload", status=400
        )

    try:
        with transaction.atomic():
            delivery, created = WebhookDelivery.objects.get_or_create(
                provider=PROVIDER,
                delivery_id=delivery_id[:120],
                defaults={"event": payload["event"][:64], "payload": payload},
            )
    except IntegrityError:  # the same delivery, racing itself
        delivery, created = (
            WebhookDelivery.objects.get(provider=PROVIDER, delivery_id=delivery_id[:120]),
            False,
        )
    if not created:
        count("duplicate")
    return Received(delivery=delivery, fresh=created)


def claim(delivery_pk: int) -> WebhookDelivery | None:
    """Takes a delivery for processing, or `None` when another worker has it
    or it is done. One conditional update, so two workers cannot both win."""
    claimed = WebhookDelivery.objects.filter(
        pk=delivery_pk, status__in=(DeliveryStatus.RECEIVED, DeliveryStatus.FAILED)
    ).update(status=DeliveryStatus.PROCESSING)
    if not claimed:
        return None
    return WebhookDelivery.objects.get(pk=delivery_pk)
