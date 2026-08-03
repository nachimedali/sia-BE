"""Proves the envelope is actually wired, not merely implemented.

test_exceptions.py unit-tests the handler; these go through the real stack, so a
misconfigured EXCEPTION_HANDLER or an unmatched URL cannot slip past.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def _assert_envelope(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) <= {"code", "message", "detail", "upgrade"}
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    return error


def test_validation_failure_returns_the_envelope() -> None:
    response = APIClient().post("/api/v1/auth/login/", {}, format="json")
    assert response.status_code == 400
    error = _assert_envelope(response)
    assert error["code"] == "validation_error"
    assert "email" in error["detail"]["fields"]


def test_unmatched_api_path_returns_the_envelope_not_html() -> None:
    """Django handles this one, not DRF — the gap A3 would otherwise leave."""
    response = APIClient().get("/api/v1/does-not-exist/")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert _assert_envelope(response)["code"] == "not_found"


def test_non_api_paths_keep_djangos_own_error_pages() -> None:
    """The admin should not start answering in JSON."""
    response = APIClient().get("/definitely-not-an-api-path/")
    assert response.status_code == 404
    assert not response["Content-Type"].startswith("application/json")


def test_request_id_is_echoed_for_tracing() -> None:
    response = APIClient().get("/api/v1/health/", HTTP_X_REQUEST_ID="trace-me-123")
    assert response["X-Request-ID"] == "trace-me-123"


def test_request_id_is_generated_when_absent() -> None:
    response = APIClient().get("/api/v1/health/")
    assert response["X-Request-ID"]


@pytest.mark.parametrize("debug", [True, False])
def test_unmatched_api_path_returns_json_in_every_environment(settings, debug: bool) -> None:
    """DEBUG=True makes Django serve its technical 404 page, bypassing
    handler404. Dev and prod must still agree on the response shape."""
    settings.DEBUG = debug

    response = APIClient().get("/api/v1/still-does-not-exist/")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert _assert_envelope(response)["code"] == "not_found"
