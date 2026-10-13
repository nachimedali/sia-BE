"""The provider call log — what the cockpit reads (`social-provider-plan.md` §9).

One row per call worth keeping: **every write and every failure**, and a
sample of successful reads (`SOCIALAPI_READ_SAMPLE_RATE`). It answers "what did
we ask the vendor, what did it say, how long did it take, and what was its
request id" — the id is what the vendor's support needs, and the only way to
connect a customer's "my post did not go out" to a specific call.

**Never holds a secret or a payload.** The path is normalised (ids replaced by
`{id}`), the body is not stored, and the key never appears. Pruned after
`SOCIALAPI_CALL_RETENTION_DAYS`; operational, not audit, so not append-only.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models


class CallKind(models.TextChoices):
    OK = "OK", "Succeeded"
    RATE_LIMITED = "RATE_LIMITED", "Rate limited"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED", "Provider quota used up"
    STORAGE_FULL = "STORAGE_FULL", "Provider storage full"
    REAUTH = "REAUTH", "Account needs reconnecting"
    SCOPE_MISSING = "SCOPE_MISSING", "Permission missing on the account"
    MISCONFIGURED = "MISCONFIGURED", "Our key or setup is wrong"
    INVALID = "INVALID", "Request refused as invalid"
    CONFLICT = "CONFLICT", "Conflicting state"
    NOT_FOUND = "NOT_FOUND", "Not found"
    UNSUPPORTED = "UNSUPPORTED", "Not supported on this platform"
    UNAVAILABLE = "UNAVAILABLE", "Provider unavailable"
    BAD_RESPONSE = "BAD_RESPONSE", "Unreadable answer"


class ProviderCall(models.Model):
    provider = models.CharField(max_length=24)
    method = models.CharField(max_length=8)
    #: `/v1/posts/{id}/metrics` — never the raw path, which carries ids.
    path = models.CharField(max_length=200)
    call_class = models.CharField(max_length=8)
    priority = models.CharField(max_length=12)
    #: Null when the call never got an HTTP answer (timeout, refused locally).
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=16, choices=CallKind.choices)
    error_code = models.CharField(max_length=80, blank=True)
    request_id = models.CharField(max_length=80, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=1)
    #: The budget the vendor reported on this response, when it did.
    rate_limit = models.PositiveIntegerField(null=True, blank=True)
    rate_remaining = models.PositiveIntegerField(null=True, blank=True)
    #: The call never left: our own budget or a missing key refused it.
    local = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["provider", "-created_at"]),
            models.Index(fields=["provider", "kind", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.method} {self.path} → {self.status_code} {self.kind}"


class DeliveryStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received, waiting to be processed"
    PROCESSING = "PROCESSING", "Being processed"
    PROCESSED = "PROCESSED", "Processed"
    IGNORED = "IGNORED", "Nothing to do"
    #: Names an account or post that is not ours: dropped, payload not kept.
    FOREIGN = "FOREIGN", "Not ours"
    #: Ours, but not matched to a row yet (polling picks it up).
    UNMATCHED = "UNMATCHED", "Not matched"
    #: Kept for a later consumer (mentions, for the S2 inbox).
    KEPT = "KEPT", "Kept for later"
    FAILED = "FAILED", "Processing failed"


class WebhookDelivery(models.Model):
    """One verified webhook delivery (SA-40, plan §7).

    Recorded **before** it is processed, keyed on the vendor's delivery id
    (stable across its retries), so a retry or a replay is recognised and
    processed once. The receiver answers as soon as this row exists; a task
    does the work.

    The payload is kept for the retention window so processing can be
    repeated and the cockpit can show what arrived — except for a delivery
    about an account or post that is not ours, whose payload is blanked
    (plan §0.5: other tenants' events are dropped, never stored).
    """

    provider = models.CharField(max_length=24)
    delivery_id = models.CharField(max_length=120)
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.RECEIVED
    )
    #: Why it was ignored, unmatched or failed, in a sentence.
    note = models.CharField(max_length=280, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-received_at"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["provider", "delivery_id"], name="unique_webhook_delivery"
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["provider", "event", "-received_at"]),
            models.Index(fields=["provider", "status", "-received_at"]),
        ]
        verbose_name_plural = "webhook deliveries"

    def __str__(self) -> str:
        return f"{self.provider} {self.event} {self.delivery_id} ({self.status})"
