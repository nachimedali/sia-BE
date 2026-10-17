"""The heads-up before a post goes live.

A scheduled post's author (auto-publish or reminder mode) is emailed once,
`POST_HEADS_UP_LEAD_MINUTES` before its slot, with the post as it will go out.
Rescheduling earns a fresh one — the old email named a time that is no longer
true.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.test import override_settings
from django.utils import timezone

from billing.models import FeatureFlag
from billing.services.flags import TRANSACTIONAL_EMAIL
from content.models import Post, PostStatus
from content.services.posts import create_post
from scheduling import heads_up, tasks
from scheduling.services import schedule_post

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)


def _scheduled(workspace: Any, user: Any, *, in_minutes: int, mode: str = "AUTO_PUBLISH") -> Post:
    post = create_post(
        workspace=workspace, author=user, master_body="First harvest, pressed the same day."
    )
    schedule_post(
        post=post,
        delivery_mode=mode,
        scheduled_at=timezone.now() + dt.timedelta(minutes=in_minutes),
        actor=user,
    )
    post.refresh_from_db()
    return post


def _heads_ups(outbox: list[Any]) -> list[Any]:
    return [e for e in outbox if e.template == "post_heads_up"]


@pytest.fixture
def clock() -> Any:
    with time_machine.travel(NOW, tick=False) as traveller:
        yield traveller


def test_a_post_inside_the_lead_time_gets_a_heads_up(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    post = _scheduled(paid_workspace, user, in_minutes=110)

    assert heads_up.send_due() == 1

    [email] = _heads_ups(outbox)
    assert email.to == user.email
    context = email.context
    assert context["post_id"] == post.pk
    assert context["countdown"] == "1 hour 50 minutes"
    assert context["lead_time"] == "2 hours"
    assert context["platforms"] == ["Instagram"]
    assert context["auto_publish"] is True
    assert context["post_url"].endswith(f"/app/posts/{post.pk}")
    assert "First harvest" in context["caption_excerpt"]
    assert "Goes live in 1 hour 50 minutes" in email.subject


def test_a_post_further_out_waits(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    _scheduled(paid_workspace, user, in_minutes=180)

    assert heads_up.send_due() == 0
    assert _heads_ups(outbox) == []

    clock.shift(dt.timedelta(minutes=61))
    assert heads_up.send_due() == 1


def test_a_post_is_announced_once(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    _scheduled(paid_workspace, user, in_minutes=90)

    heads_up.send_due()
    clock.shift(dt.timedelta(minutes=5))
    heads_up.send_due()

    assert len(_heads_ups(outbox)) == 1


def test_rescheduling_earns_a_fresh_heads_up(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    post = _scheduled(paid_workspace, user, in_minutes=90)
    heads_up.send_due()

    Post.objects.filter(pk=post.pk).update(scheduled_at=NOW + dt.timedelta(minutes=60))
    heads_up.send_due()

    assert len(_heads_ups(outbox)) == 2
    assert _heads_ups(outbox)[-1].context["countdown"] == "1 hour"


def test_a_post_already_due_is_not_announced(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    """Past its slot it is the publisher's (or the reminder's) business; a
    heads-up that arrives after the fact says something false."""
    post = _scheduled(paid_workspace, user, in_minutes=30)
    Post.objects.filter(pk=post.pk).update(scheduled_at=NOW - dt.timedelta(minutes=1))

    assert heads_up.send_due() == 0


def test_only_scheduled_posts_are_announced(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    post = _scheduled(paid_workspace, user, in_minutes=60)
    Post.objects.filter(pk=post.pk).update(status=PostStatus.DRAFT)

    assert heads_up.send_due() == 0


def test_a_reminder_mode_post_says_the_author_posts_it(
    clock: Any, workspace: Any, user: Any, outbox: list[Any]
) -> None:
    _scheduled(workspace, user, in_minutes=60, mode="REMINDER")

    heads_up.send_due()

    [email] = _heads_ups(outbox)
    assert email.context["auto_publish"] is False


def test_a_workspace_with_an_unknown_time_zone_is_still_announced(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    """One bad zone string must not stop the scan for every post after it."""
    paid_workspace.timezone = "Mars/Olympus_Mons"
    paid_workspace.save(update_fields=["timezone"])
    _scheduled(paid_workspace, user, in_minutes=60)

    assert heads_up.send_due() == 1
    assert _heads_ups(outbox)[0].context["timezone"] == "UTC"


@override_settings(POST_HEADS_UP_LEAD_MINUTES=24 * 60)
def test_the_lead_time_is_a_setting(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    _scheduled(paid_workspace, user, in_minutes=20 * 60)

    assert heads_up.send_due() == 1
    assert _heads_ups(outbox)[0].context["lead_time"] == "1 day"


def test_flag_off_sends_nothing(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    FeatureFlag.objects.create(organization=None, key=TRANSACTIONAL_EMAIL, enabled=False)
    _scheduled(paid_workspace, user, in_minutes=60)

    assert heads_up.send_due() == 0
    assert _heads_ups(outbox) == []


def test_the_beat_task_runs_the_scan(
    clock: Any, paid_workspace: Any, user: Any, social_account: Any, outbox: list[Any]
) -> None:
    _scheduled(paid_workspace, user, in_minutes=60)

    tasks.remind_upcoming_posts.apply()

    assert len(_heads_ups(outbox)) == 1


def test_the_task_is_routed_to_the_transactional_queue() -> None:
    from config.celery import app

    route = app.amqp.router.route({}, "scheduling.tasks.remind_upcoming_posts")
    assert route["queue"].name == "remind_q"


def test_the_scan_is_on_the_beat_schedule() -> None:
    from django.conf import settings

    tasks_scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    assert "scheduling.tasks.remind_upcoming_posts" in tasks_scheduled
