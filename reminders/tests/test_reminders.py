"""Reminder lifecycle: arm → send → confirm/snooze/skip (design.md §8.5, D4/D5,
implementation.md Phase 8)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.utils import timezone
from rest_framework.test import APIClient

from content.models import PostStatus
from content.services.posts import create_post
from reminders import services, tasks
from reminders.models import Reminder, ReminderState
from scheduling.services import schedule_post

pytestmark = pytest.mark.django_db


def _armed_reminder(workspace: Any, user: Any, *, in_minutes: int = 2) -> Reminder:
    post = create_post(workspace=workspace, author=user, master_body="Check out our new drop")
    scheduled_at = timezone.now() + dt.timedelta(minutes=in_minutes)
    schedule_post(post=post, delivery_mode="REMINDER", scheduled_at=scheduled_at)
    return Reminder.objects.get(post=post)


def _token_from_outbox(outbox: list[Any]) -> str:
    packet_url = outbox[-1].context["packet_url"]
    return packet_url.rsplit("/r/", 1)[-1]


def _public_client() -> APIClient:
    # Deliberately not `auth_client` — these endpoints carry no session at
    # all (design.md §8.5: "/r/[token] requires no login by design").
    return APIClient()


# -----------------------------------------------------------------------------
# test_post_scheduled_now_plus_two_minutes_sends_on_time
# -----------------------------------------------------------------------------
def test_post_scheduled_now_plus_two_minutes_sends_on_time(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        reminder = _armed_reminder(workspace, user, in_minutes=2)
        assert services.send_due() == 0  # not due yet
        assert outbox == []

        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        assert services.send_due() == 1

    reminder.refresh_from_db()
    assert reminder.state == ReminderState.SENT
    assert reminder.sent_at is not None
    assert reminder.token_hash is not None
    assert len(outbox) == 1
    assert outbox[0].to == user.email
    assert "/r/" in outbox[0].context["packet_url"]


# -----------------------------------------------------------------------------
# test_reminder_token_exposes_only_its_own_packet
# -----------------------------------------------------------------------------
def test_reminder_token_exposes_only_its_own_packet(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    mine = _armed_reminder(workspace, user, in_minutes=0)
    other_post = create_post(workspace=workspace, author=user, master_body="A different post")
    other_post.scheduled_at = timezone.now()
    other_post.save(update_fields=["scheduled_at"])
    other = Reminder.objects.create(post=other_post, send_at=other_post.scheduled_at)

    services.send_due()
    assert len(outbox) == 2
    my_token = _token_from_outbox([e for e in outbox if e.context["post"].id == mine.post_id])

    client = _public_client()
    response = client.get(f"/api/v1/reminders/{my_token}/packet/")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == mine.id
    assert body["id"] != other.id

    # An unknown / garbage token gets the same 404 a wrong-purpose one would —
    # nothing distinguishes "exists but not yours" from "doesn't exist".
    bogus = client.get("/api/v1/reminders/not-a-real-token/packet/")
    assert bogus.status_code == 404


# -----------------------------------------------------------------------------
# test_reminder_token_single_use_and_expires
# -----------------------------------------------------------------------------
def test_reminder_token_single_use_and_expires(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        _armed_reminder(workspace, user)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        services.send_due()
        token = _token_from_outbox(outbox)
        client = _public_client()

        # Single-use: confirming twice only succeeds once.
        first = client.post(f"/api/v1/reminders/{token}/confirm/")
        assert first.status_code == 200
        second = client.post(f"/api/v1/reminders/{token}/confirm/")
        assert second.status_code == 404

    # Expires: a token past its TTL from send is rejected even mid-lifecycle.
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        stale = _armed_reminder(workspace, user, in_minutes=2)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        services.send_due()
        stale_token = _token_from_outbox(outbox)

        traveller.shift(Reminder.TOKEN_TTL + dt.timedelta(days=1))
        client = _public_client()
        response = client.get(f"/api/v1/reminders/{stale_token}/packet/")
        assert response.status_code == 404

    stale.refresh_from_db()
    assert stale.state == ReminderState.EXPIRED


# -----------------------------------------------------------------------------
# test_confirm_flips_post_to_published
# -----------------------------------------------------------------------------
def test_confirm_flips_post_to_published(workspace: Any, user: Any, outbox: list[Any]) -> None:
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        reminder = _armed_reminder(workspace, user)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        services.send_due()
    token = _token_from_outbox(outbox)

    response = _public_client().post(f"/api/v1/reminders/{token}/confirm/")

    assert response.status_code == 200
    assert response.json()["state"] == ReminderState.CONFIRMED
    reminder.refresh_from_db()
    assert reminder.state == ReminderState.CONFIRMED
    assert reminder.confirmed_at is not None
    reminder.post.refresh_from_db()
    assert reminder.post.status == PostStatus.PUBLISHED


# -----------------------------------------------------------------------------
# test_snooze_reschedules_and_skip_terminates
# -----------------------------------------------------------------------------
def test_snooze_reschedules_and_skip_terminates(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        reminder = _armed_reminder(workspace, user)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        services.send_due()
    token = _token_from_outbox(outbox)

    new_time = timezone.now() + dt.timedelta(hours=3)
    snooze_response = _public_client().post(
        f"/api/v1/reminders/{token}/snooze/", {"snoozed_to": new_time.isoformat()}, format="json"
    )
    assert snooze_response.status_code == 200
    assert snooze_response.json()["state"] == ReminderState.SNOOZED
    reminder.refresh_from_db()
    assert reminder.state == ReminderState.SNOOZED
    assert reminder.send_at == new_time
    reminder.post.refresh_from_db()
    assert reminder.post.scheduled_at == new_time
    assert reminder.post.status == PostStatus.REMINDER_ARMED  # unchanged by snoozing

    # The same (still-usable, snoozed) token now terminates the reminder.
    skip_response = _public_client().post(f"/api/v1/reminders/{token}/skip/")
    assert skip_response.status_code == 200
    assert skip_response.json()["state"] == ReminderState.SKIPPED
    reminder.refresh_from_db()
    assert reminder.state == ReminderState.SKIPPED
    reminder.post.refresh_from_db()
    assert reminder.post.status == PostStatus.DRAFT
    assert reminder.post.scheduled_at is None


# -----------------------------------------------------------------------------
# Supporting coverage
# -----------------------------------------------------------------------------
def test_expire_stale_sweeps_reminders_nobody_ever_revisited(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        reminder = _armed_reminder(workspace, user)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        services.send_due()

        traveller.shift(Reminder.TOKEN_TTL + dt.timedelta(days=1))
        assert services.expire_stale() == 1

    reminder.refresh_from_db()
    assert reminder.state == ReminderState.EXPIRED


def test_beat_task_wrappers_call_through_to_the_services(
    workspace: Any, user: Any, outbox: list[Any]
) -> None:
    """Thin Celery wrappers (design.md §5.1, implementation.md §4.1) — each
    body is one call, exercised here without a broker."""
    with time_machine.travel("2026-08-10 09:00:00+00:00", tick=False) as traveller:
        reminder = _armed_reminder(workspace, user)
        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        assert tasks.send_due_reminders() == 1

        traveller.shift(Reminder.TOKEN_TTL + dt.timedelta(days=1))
        assert tasks.expire_stale_reminders() == 1

    reminder.refresh_from_db()
    assert reminder.state == ReminderState.EXPIRED


def test_reminder_calendar_list_is_workspace_scoped(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    mine = _armed_reminder(workspace, user)

    other_user = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_workspace = provision_workspace(other_user, name="Someone Else's")
    _armed_reminder(other_workspace, other_user)

    response = auth_client.get("/api/v1/reminders/")

    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["results"]]
    assert ids == [mine.id]
