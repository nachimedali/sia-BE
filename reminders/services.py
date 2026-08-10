"""Reminder lifecycle (design.md §8.5, D4/D5, implementation.md Phase 8).

`arm_reminder` is called only from `scheduling.services.schedule_post` —
Reminder rows exist only for `REMINDER`-mode posts. Beat's `send_due`
(`remind_q`) is what actually mints the token and emails it; everything
after that — confirm/snooze/skip — is a token-scoped action reached from
`/r/[token]` with no workspace auth, gated only by `resolve_token`.
"""

from __future__ import annotations

import datetime as dt
import secrets

from django.conf import settings
from django.utils import timezone

from common.mail import Email, get_mail_sender
from content.models import Post, PostStatus
from reminders.models import Reminder, ReminderState


def arm_reminder(post: Post, send_at: dt.datetime) -> Reminder:
    return Reminder.objects.create(post=post, send_at=send_at, state=ReminderState.ARMED)


def send_due(*, now: dt.datetime | None = None) -> int:
    """Beat's scan (design.md §5.1: `remind_q`). Mints the token here, at the
    moment the email actually goes out — not at arm time — so no raw value is
    ever at rest before it is sent (design.md A25's shape, applied to a
    reminder that may sit ARMED for days before it is due).
    """
    moment = now or timezone.now()
    due = Reminder.objects.select_related("post", "post__author").filter(
        state__in=[ReminderState.ARMED, ReminderState.SNOOZED], send_at__lte=moment
    )
    count = 0
    for reminder in due:
        _send_one(reminder, moment)
        count += 1
    return count


def _send_one(reminder: Reminder, moment: dt.datetime) -> None:
    raw = secrets.token_urlsafe(32)
    reminder.token_hash = Reminder.hash_token(raw)
    reminder.state = ReminderState.SENT
    reminder.sent_at = moment
    reminder.save(update_fields=["token_hash", "state", "sent_at", "updated_at"])

    post = reminder.post
    packet_url = f"{settings.SITE_URL}/r/{raw}"
    get_mail_sender().send(
        Email(
            to=post.author.email,
            subject="Time to post",
            template="reminder_packet",
            context={"post": post, "packet_url": packet_url},
        )
    )


def resolve_token(raw: str) -> Reminder | None:
    """The one lookup every public `/r/[token]` view goes through. Returns
    `None` for an unknown, wrong-purpose, or expired token — the caller can't
    tell those apart, which is the point (design.md: the token "must expose
    nothing beyond that post's packet").
    """
    reminder = (
        Reminder.objects.select_related("post", "post__workspace", "post__author")
        .filter(token_hash=Reminder.hash_token(raw))
        .first()
    )
    if reminder is None:
        return None
    if reminder.state in {ReminderState.SENT, ReminderState.SNOOZED} and not reminder.is_usable:
        # Chronologically past its TTL but no sweep has caught it yet —
        # resolve the clock now rather than making every caller re-derive
        # `is_usable` (billing.services.entitlements's trial-lapse check is
        # the same shape: self-healing state that does not depend on a
        # periodic task having run).
        reminder.state = ReminderState.EXPIRED
        reminder.save(update_fields=["state", "updated_at"])
    if not reminder.is_usable:
        return None
    return reminder


def confirm(reminder: Reminder) -> Reminder:
    reminder.state = ReminderState.CONFIRMED
    reminder.confirmed_at = timezone.now()
    reminder.save(update_fields=["state", "confirmed_at", "updated_at"])

    post = reminder.post
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status", "updated_at"])
    return reminder


def snooze(reminder: Reminder, snoozed_to: dt.datetime) -> Reminder:
    reminder.state = ReminderState.SNOOZED
    reminder.snoozed_to = snoozed_to
    reminder.send_at = snoozed_to
    reminder.save(update_fields=["state", "snoozed_to", "send_at", "updated_at"])

    post = reminder.post
    post.scheduled_at = snoozed_to
    post.save(update_fields=["scheduled_at", "updated_at"])
    return reminder


def skip(reminder: Reminder) -> Reminder:
    reminder.state = ReminderState.SKIPPED
    reminder.save(update_fields=["state", "updated_at"])

    # "Skip terminates" (implementation.md Phase 8) — the post returns to
    # DRAFT rather than a failure-flavoured status: nothing went wrong, the
    # user chose not to post this at this time, and it stays editable.
    post = reminder.post
    post.status = PostStatus.DRAFT
    post.delivery_mode = ""
    post.scheduled_at = None
    post.save(update_fields=["status", "delivery_mode", "scheduled_at", "updated_at"])
    return reminder


def expire_stale() -> int:
    """Daily sweep (mirrors `billing.tasks.expire_trials`'s shape) — catches
    reminders nobody ever revisited to trigger `resolve_token`'s lazy check.
    A bulk `.update()` bypasses `auto_now`, so `updated_at` is set explicitly.
    """
    cutoff = timezone.now() - Reminder.TOKEN_TTL
    return Reminder.objects.filter(
        state__in=[ReminderState.SENT, ReminderState.SNOOZED], sent_at__lt=cutoff
    ).update(state=ReminderState.EXPIRED, updated_at=timezone.now())
