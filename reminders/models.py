"""Reminder delivery — the Free-tier path (design.md §6.3, §8.5, D4/D5).

The token is minted only at send time, in `reminders.services.send_due`, and
only its SHA-256 hash is ever persisted (`accounts.EmailToken`'s precedent,
design.md A25) — the raw value exists once, in the email. Until sent,
`token_hash` is null: many un-sent reminders coexist under the unique
constraint the same way `PostTarget.idempotency_key` does
(content/models.py), because Postgres does not treat repeated NULLs as
duplicates.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import ClassVar

from django.db import models
from django.utils import timezone


class ReminderChannel(models.TextChoices):
    EMAIL = "EMAIL", "Email"


class ReminderState(models.TextChoices):
    ARMED = "ARMED", "Armed"
    SENT = "SENT", "Sent"
    CONFIRMED = "CONFIRMED", "Confirmed"
    SNOOZED = "SNOOZED", "Snoozed"
    SKIPPED = "SKIPPED", "Skipped"
    EXPIRED = "EXPIRED", "Expired"


class Reminder(models.Model):
    """design.md §6.3. A plain FK to `Post`, not one-to-one: skipping and
    rescheduling a post arms a fresh row rather than resurrecting a
    terminated one, so a post's reminder history stays inspectable.

    Armed by `scheduling.services.schedule_post` when `delivery_mode=
    REMINDER`; sent by Beat's `reminders.tasks.send_due_reminders`
    (`remind_q`, design.md §5.1).
    """

    # From `sent_at`, not `send_at` — the clock the user experiences the link
    # against starts when it actually landed in their inbox.
    TOKEN_TTL: ClassVar[dt.timedelta] = dt.timedelta(days=14)

    post = models.ForeignKey("content.Post", on_delete=models.CASCADE, related_name="reminders")
    channel = models.CharField(
        max_length=8, choices=ReminderChannel.choices, default=ReminderChannel.EMAIL
    )
    send_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    snoozed_to = models.DateTimeField(null=True, blank=True)
    # Null until send time (see module docstring); unique so a hash collision
    # across two reminders is a database-level impossibility, not just an
    # application-level assumption.
    token_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    state = models.CharField(
        max_length=16, choices=ReminderState.choices, default=ReminderState.ARMED
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["send_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["state", "send_at"]),
            models.Index(fields=["post", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Reminder for post {self.post_id} ({self.state})"

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def is_usable(self) -> bool:
        """True while the emailed link may still act: delivered (`SENT` or
        `SNOOZED` — snoozing keeps the same link live for its new time) and
        inside the token's TTL. `ARMED` has no live token yet (module
        docstring); `CONFIRMED`/`SKIPPED`/`EXPIRED` are terminal.
        """
        if self.state not in {ReminderState.SENT, ReminderState.SNOOZED}:
            return False
        if self.sent_at is None:
            return False
        return timezone.now() <= self.sent_at + self.TOKEN_TTL
