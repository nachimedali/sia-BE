"""Queue smoke task."""

from common.tasks import ping


def test_ping_returns_its_queue() -> None:
    assert ping.run("publish_q") == {"pong": True, "queue": "publish_q"}


def test_ping_is_registered_under_a_stable_name() -> None:
    # Routing in config/celery.py is keyed on task names, so the name is part
    # of the contract, not an implementation detail.
    assert ping.name == "common.tasks.ping"


# -----------------------------------------------------------------------------
# Phase 1's beat jobs (P1-08, P1-10)
# -----------------------------------------------------------------------------
def test_phase_one_beat_jobs_are_scheduled_and_routed() -> None:
    """A task with a route and no schedule never runs, and nothing fails —
    `content.tasks.recurrence_materialise` shipped that way until an E2E run
    noticed nothing was being materialised. Both halves are asserted because
    either one alone is silent."""
    from django.conf import settings

    from config.celery import app

    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    for task in ("content.tasks.prune_post_revisions", "content.tasks.recurrence_materialise"):
        assert task in scheduled, f"{task} is routed but never scheduled"
        assert app.amqp.router.route({}, task)["queue"].name == "metrics_q"


# -----------------------------------------------------------------------------
# Phase 7's beat job (P7-01, P7-02)
# -----------------------------------------------------------------------------
def test_learn_is_scheduled_and_routed_to_its_own_queue() -> None:
    """Same two halves as above, for the same reason.

    The extra claim here is the *queue*: Learn reads a whole trailing window
    and may wait on a provider, so a routing slip that put it on `publish_q`
    would not fail anything — it would occupy, for minutes at a time, the pool
    that has to hit 09:00 exactly.
    """
    from django.conf import settings

    from config.celery import app

    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    assert "learn.tasks.close_due_campaigns" in scheduled

    for task in ("learn.tasks.close_due_campaigns", "learn.tasks.run_learn_for_campaign"):
        assert app.amqp.router.route({}, task)["queue"].name == "analyze_q"
