"""Health probe (design.md §7.2, A7)."""

from __future__ import annotations

from unittest import mock

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

HEALTH_URL = "/api/v1/health/"


def test_health_reports_each_dependency() -> None:
    response = APIClient().get(HEALTH_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"

    # Each dependency is reported separately, not rolled into one boolean.
    assert set(body["checks"]) == {"postgres", "redis"}
    for check in body["checks"].values():
        assert check["status"] == "ok"
        assert isinstance(check["latency_ms"], (int, float))

    assert "providers" in body


def test_health_requires_no_authentication() -> None:
    """A probe that needs a token is a probe that fails during an auth outage."""
    assert APIClient().get(HEALTH_URL).status_code == 200


def test_health_503_when_db_down() -> None:
    with mock.patch("common.health.check_postgres", side_effect=OSError("connection refused")):
        response = APIClient().get(HEALTH_URL)

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    assert "postgres" in error["message"]

    # The per-dependency report survives inside the envelope (design.md A3),
    # so probes and dashboards still see which dependency failed.
    detail = error["detail"]
    assert detail["status"] == "degraded"
    assert detail["checks"]["postgres"]["status"] == "error"
    assert detail["checks"]["redis"]["status"] == "ok"


def test_health_503_when_redis_down() -> None:
    with mock.patch("common.health.check_redis", side_effect=OSError("no route to host")):
        response = APIClient().get(HEALTH_URL)

    assert response.status_code == 503
    assert response.json()["error"]["detail"]["checks"]["redis"]["status"] == "error"


def test_provider_failures_do_not_affect_the_status_code() -> None:
    """design.md A7 — a flaky vendor must not take the app down."""
    response = APIClient().get(HEALTH_URL)
    assert response.status_code == 200
    assert "providers" in response.json()


BEAT_URL = "/api/v1/health/beat/"


def test_beat_health_503_until_the_heartbeat_runs_then_200() -> None:
    from common.tasks import beat_heartbeat

    with (
        mock.patch("common.health.get_redis") as redis,
        mock.patch("common.tasks.get_redis", redis),
    ):
        redis.return_value.get.return_value = None
        stale = APIClient().get(BEAT_URL)
        assert stale.status_code == 503
        assert stale.json()["error"]["code"] == "beat_stale"

        beat_heartbeat()
        _, value = redis.return_value.set.call_args.args
        redis.return_value.get.return_value = value
        assert APIClient().get(BEAT_URL).status_code == 200


def test_beat_health_503_when_the_heartbeat_is_old() -> None:
    import time

    with mock.patch("common.health.get_redis") as redis:
        redis.return_value.get.return_value = str(int(time.time()) - 3600)
        assert APIClient().get(BEAT_URL).status_code == 503
