"""An edit to a post already scheduled sends it back to review, and keeps its
time as the proposal it is rescheduled from once re-approved (L-2). This is
what stops a clip added by the post editor's Animate from publishing unreviewed."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from content.models import PostStatus
from content.services.posts import create_post, edit_post
from reminders.models import Reminder
from scheduling.services import schedule_post
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


def test_a_scheduled_post_edited_goes_back_to_review_with_its_slot_held(
    advanced_workspace: Any, user: Any, admin_user: Any, media_asset: Any
) -> None:
    when = (timezone.now() + dt.timedelta(days=3)).replace(microsecond=0)
    post = create_post(workspace=advanced_workspace, author=user, master_body="Harvest week")
    approvals.submit_for_review(post, actor=user)
    approvals.approve(post, actor=admin_user)
    post = schedule_post(post=post, delivery_mode="REMINDER", scheduled_at=when, actor=admin_user)
    assert post.status == PostStatus.REMINDER_ARMED
    approvals.unlock(post, actor=admin_user)

    edited = edit_post(post, author=user, media=[(media_asset, "A new clip")])

    assert edited.status == PostStatus.PENDING_REVIEW
    assert edited.scheduled_at is None and edited.delivery_mode == ""
    assert edited.proposed_scheduled_at == when and edited.proposed_delivery_mode == "REMINDER"
    assert not Reminder.objects.filter(post=post).exclude(state="SKIPPED").exists()

    rescheduled = approvals.approve(edited, actor=admin_user)

    assert rescheduled.status == PostStatus.REMINDER_ARMED
    assert rescheduled.scheduled_at == when
