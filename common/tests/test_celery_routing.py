"""Queue topology (design.md §5.1).

Publishing has a hard deadline; trend extraction does not. If a trends task were
routed onto publish_q it would sit in the pool that has to hit 09:00 exactly, so
routing is asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from config.celery import QUEUE_NAMES, app


def test_every_queue_is_declared() -> None:
    declared = {queue.name for queue in app.conf.task_queues}
    assert declared == {
        "publish_q",
        "remind_q",
        "media_q",
        "ai_q",
        "metrics_q",
        "trends_q",
        # P2-12.
        "notify_q",
    }
    assert len(QUEUE_NAMES) == 7


@pytest.mark.parametrize(
    ("task_name", "expected_queue"),
    [
        ("scheduling.tasks.publish_due_posts", "publish_q"),
        ("content.tasks.publish_target", "publish_q"),
        ("reminders.tasks.send_reminder", "remind_q"),
        ("scheduling.tasks.remind_due", "remind_q"),
        ("content.tasks.media_ingest", "media_q"),
        # Periodic content bookkeeping shares the pool the billing sweeps use;
        # what matters is that it is *declared*, not that it is alone (P1-08,
        # P1-10).
        ("content.tasks.prune_post_revisions", "metrics_q"),
        ("content.tasks.recurrence_materialise", "metrics_q"),
        ("ai.tasks.video_render", "media_q"),
        ("ai.tasks.generate_image", "ai_q"),
        ("analytics.tasks.poll_metrics", "metrics_q"),
        ("trends.tasks.extract_recipes", "trends_q"),
        ("notifications.tasks.deliver_notification", "notify_q"),
    ],
)
def test_tasks_route_to_their_queue(task_name: str, expected_queue: str) -> None:
    route = app.amqp.router.route({}, task_name)
    assert route["queue"].name == expected_queue


def test_notifications_do_not_share_the_reminder_pool() -> None:
    """A busy thread can fan out to a whole team at once. Sharing `remind_q`
    would let that burst sit in front of the reminder telling somebody to
    publish at 09:00 (P2-12)."""
    notify = app.amqp.router.route({}, "notifications.tasks.deliver_notification")["queue"].name
    remind = app.amqp.router.route({}, "reminders.tasks.send_reminder")["queue"].name
    assert notify == "notify_q"
    assert notify != remind


def test_video_rendering_is_isolated_from_the_ai_pool() -> None:
    """Video is long-running and provider-bound; sharing ai_q would let one
    render starve a pool sized for fast text and image calls."""
    video = app.amqp.router.route({}, "ai.tasks.video_render")["queue"].name
    image = app.amqp.router.route({}, "ai.tasks.generate_image")["queue"].name
    assert video == "media_q"
    assert image == "ai_q"
    assert video != image


def test_unrouted_tasks_do_not_land_on_a_real_queue() -> None:
    """An unrouted task is a routing bug and should be visible, not absorbed by
    one of the six pools."""
    route = app.amqp.router.route({}, "some.unregistered.task")
    assert route["queue"].name == "default"
    assert route["queue"].name not in QUEUE_NAMES
