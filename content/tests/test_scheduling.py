"""`POST /posts/{id}/schedule/` (design.md §7, §8.5, implementation.md Phase 8)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from content.models import PostStatus
from content.services.posts import create_post
from reminders.models import Reminder, ReminderState

pytestmark = pytest.mark.django_db


def _schedule_url(post_id: int) -> str:
    return f"/api/v1/posts/{post_id}/schedule/"


def test_schedule_reminder_arms_a_reminder(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="New drop is live")
    scheduled_at = timezone.now() + dt.timedelta(minutes=2)

    response = auth_client.post(
        _schedule_url(post.id),
        {"delivery_mode": "REMINDER", "scheduled_at": scheduled_at.isoformat()},
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == PostStatus.REMINDER_ARMED
    assert body["delivery_mode"] == "REMINDER"

    reminder = Reminder.objects.get(post=post)
    assert reminder.state == ReminderState.ARMED
    assert reminder.send_at == scheduled_at


def test_scheduling_beyond_plan_horizon_rejected_402(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    """Free plan's `scheduling_horizon_days` is 7 (`seed_plans`, design.md §4.1)."""
    post = create_post(workspace=workspace, author=user, master_body="Too far out")
    too_far = timezone.now() + dt.timedelta(days=8)

    response = auth_client.post(
        _schedule_url(post.id),
        {"delivery_mode": "REMINDER", "scheduled_at": too_far.isoformat()},
        format="json",
    )

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert "upgrade" in error
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT
    assert not Reminder.objects.filter(post=post).exists()


def test_schedule_auto_publish_requires_the_feature(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    """D4: Free tier is reminders-only. `auto_publish` is a Pro/Advanced
    feature (`seed_plans`) — Free choosing it is a 402, not a silent no-op."""
    post = create_post(workspace=workspace, author=user, master_body="Auto-publish me")
    soon = timezone.now() + dt.timedelta(days=1)

    response = auth_client.post(
        _schedule_url(post.id),
        {"delivery_mode": "AUTO_PUBLISH", "scheduled_at": soon.isoformat()},
        format="json",
    )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_available"


def test_schedule_auto_publish_sets_scheduled_on_a_plan_with_the_feature(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """A connected account is a precondition as of Phase 9 — scheduling now
    builds one `PostTarget` per account, so there has to be one to build."""
    post = create_post(workspace=paid_workspace, author=user, master_body="Auto-publish me")
    soon = timezone.now() + dt.timedelta(days=1)

    response = auth_client.post(
        _schedule_url(post.id),
        {"delivery_mode": "AUTO_PUBLISH", "scheduled_at": soon.isoformat()},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.SCHEDULED
    assert not Reminder.objects.filter(post=post).exists()


def test_schedule_rejects_a_past_scheduled_at(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Yesterday's news")
    past = timezone.now() - dt.timedelta(hours=1)

    response = auth_client.post(
        _schedule_url(post.id),
        {"delivery_mode": "REMINDER", "scheduled_at": past.isoformat()},
        format="json",
    )

    assert response.status_code == 400


# -----------------------------------------------------------------------------
# The approval gate (design.md §8.8, implementation.md Phase 13)
# -----------------------------------------------------------------------------
def test_schedule_refuses_an_unapproved_post_when_approval_is_required(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    soon = timezone.now() + dt.timedelta(days=1)

    response = client_as(contributor_user).post(
        _schedule_url(post.id),
        {"delivery_mode": "REMINDER", "scheduled_at": soon.isoformat()},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT
    assert not Reminder.objects.filter(post=post).exists()


def test_schedule_accepts_an_approved_post_when_approval_is_required(
    client_as: Any,
    advanced_workspace: Any,
    contributor_user: Any,
    admin_user: Any,
    advanced_social_account: Any,
) -> None:
    from workspaces.services import approvals

    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    approvals.approve(post, actor=admin_user)
    soon = timezone.now() + dt.timedelta(days=1)

    response = client_as(contributor_user).post(
        _schedule_url(post.id),
        {"delivery_mode": "AUTO_PUBLISH", "scheduled_at": soon.isoformat()},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.SCHEDULED
