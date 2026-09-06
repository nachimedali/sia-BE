"""The `comment.received` webhook (P0-35, L-4a).

**Why a webhook rather than a tighter poll.** Zernio caches both comment read
endpoints for ten minutes and its documentation says plainly not to poll them.
Advanced's "near-real-time" tier is therefore a subscription, not a shorter
interval — a one-minute poll would return the same cached page nine times out
of ten and spend the vendor relationship for nothing.

Verified by HMAC over the raw body, checked *before* the payload is parsed, on
the same reasoning as the Stripe handler: an unverified body is untrusted
input, and parsing it first widens the attack surface to the JSON decoder.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from analytics.models import AudienceCaptureState, AudienceComment, Availability
from analytics.services.sentiment import classify_comments
from common.timestamps import parse_or_none
from content.models import PostTarget, PostTargetState

logger = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    """Raised before parsing. The caller answers 400, not 403 — a bad
    signature is a payload problem the sender should stop resending, and a
    403 invites a retry loop."""


def verify(body: bytes, signature: str) -> None:
    secret = getattr(settings, "ZERNIO_WEBHOOK_SECRET", "")
    if not secret:
        raise WebhookSignatureError("No webhook secret is configured.")

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Constant-time: a plain `==` leaks the correct prefix through timing.
    if not hmac.compare_digest(expected, signature):
        raise WebhookSignatureError("Signature does not match.")


def ingest_comment_event(payload: dict[str, Any]) -> bool:
    """Store one delivered comment. Returns whether a row was written.

    Idempotent on `(post_target, external_id)`, because a webhook that is not
    idempotent is a webhook that duplicates on every redelivery — and every
    provider redelivers.

    A comment for a post this deployment does not know is dropped rather than
    stored orphaned: the provider profile can carry accounts we did not
    publish through, and their audiences are not ours to record.
    """
    external_id = str(payload.get("commentId") or payload.get("id") or "")
    provider_post_id = str(payload.get("postId") or "")
    if not external_id or not provider_post_id:
        return False

    target = (
        PostTarget.objects.filter(
            provider_post_id=provider_post_id, state=PostTargetState.PUBLISHED
        )
        .select_related("post__workspace")
        .first()
    )
    if target is None:
        logger.info("comment webhook for an unknown post", extra={"post": provider_post_id})
        return False

    body = str(payload.get("text") or payload.get("body") or "")
    verdict = classify_comments([body])[0]

    _comment, created = AudienceComment.objects.get_or_create(
        post_target=target,
        external_id=external_id,
        defaults={
            "author": str(payload.get("author") or payload.get("username") or ""),
            "body": body,
            "sentiment": verdict.sentiment,
            "sentiment_score": verdict.score,
            "posted_at": parse_or_none(payload.get("createdAt")) or timezone.now(),
            "reactions": payload.get("reactions") or {},
            "availability": Availability.MEASURED,
        },
    )
    if created:
        AudienceCaptureState.objects.update_or_create(
            post_target=target, defaults={"last_captured_at": timezone.now()}
        )
    return created
