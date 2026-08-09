"""Publish idempotency (design.md §11, I9). No publish task exists yet
(Phase 9) — see content/services/idempotency.py for why these tests call the
service directly."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from content.models import Platform, PostTarget
from content.services.idempotency import assign_idempotency_key, idempotency_key_for
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


def _target(workspace: Any, user: Any) -> PostTarget:
    post = create_post(workspace=workspace, author=user)
    return PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)


def test_idempotency_key_stable_across_retries(workspace: Any, user: Any) -> None:
    """The named Phase 6 test. A Celery retry calls this again with the same
    inputs and must get the same key — that sameness is what lets the worker
    recognise "I've tried this exact attempt before"."""
    target = _target(workspace, user)
    scheduled_at = timezone.now()

    first_attempt = idempotency_key_for(target.id, scheduled_at)
    retry_attempt = idempotency_key_for(target.id, scheduled_at)

    assert first_attempt == retry_attempt


def test_idempotency_key_differs_per_target_and_per_schedule(workspace: Any, user: Any) -> None:
    target = _target(workspace, user)
    other_target = _target(workspace, user)
    scheduled_at = timezone.now()
    later = scheduled_at + dt.timedelta(minutes=1)

    assert idempotency_key_for(target.id, scheduled_at) != idempotency_key_for(
        other_target.id, scheduled_at
    )
    assert idempotency_key_for(target.id, scheduled_at) != idempotency_key_for(target.id, later)


def test_assign_persists_the_key_on_the_row(workspace: Any, user: Any) -> None:
    target = _target(workspace, user)
    scheduled_at = timezone.now()

    key = assign_idempotency_key(target, scheduled_at)

    target.refresh_from_db()
    assert target.idempotency_key == key


def test_assign_is_idempotent_itself(workspace: Any, user: Any) -> None:
    """Calling assign twice for the same schedule — e.g. a re-queued task
    reaching this step again — must not churn the stored value or the row's
    `updated_at`."""
    target = _target(workspace, user)
    scheduled_at = timezone.now()

    first = assign_idempotency_key(target, scheduled_at)
    updated_after_first = target.updated_at

    target.refresh_from_db()
    second = assign_idempotency_key(target, scheduled_at)

    assert second == first
    assert target.updated_at == updated_after_first


def test_rescheduling_produces_a_new_key(workspace: Any, user: Any) -> None:
    """A reschedule is a different publish, not a retry of the old one — it
    must not be mistaken for a duplicate of the original attempt."""
    target = _target(workspace, user)
    original = assign_idempotency_key(target, timezone.now())

    rescheduled = assign_idempotency_key(target, timezone.now() + dt.timedelta(days=1))

    assert rescheduled != original
