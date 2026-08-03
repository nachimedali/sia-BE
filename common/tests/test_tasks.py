"""Queue smoke task."""

from common.tasks import ping


def test_ping_returns_its_queue() -> None:
    assert ping.run("publish_q") == {"pong": True, "queue": "publish_q"}


def test_ping_is_registered_under_a_stable_name() -> None:
    # Routing in config/celery.py is keyed on task names, so the name is part
    # of the contract, not an implementation detail.
    assert ping.name == "common.tasks.ping"
