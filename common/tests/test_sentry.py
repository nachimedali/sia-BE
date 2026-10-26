"""Sentry error reporting (F14): off without a DSN, tagged with the request id."""

from __future__ import annotations

from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from common import observability
from common.logging import request_id_var, user_id_var, workspace_id_var


def test_the_suite_never_reports():
    from django.conf import settings

    assert settings.SENTRY_DSN == ""


@override_settings(SENTRY_DSN="")
def test_without_a_dsn_nothing_is_initialised():
    with mock.patch("sentry_sdk.init") as init:
        assert observability.init_sentry() is False
    init.assert_not_called()


@override_settings(
    SENTRY_DSN="https://key@o1.ingest.de.sentry.io/2",
    SENTRY_ENVIRONMENT="beta",
    SENTRY_TRACES_SAMPLE_RATE=0.25,
    SENTRY_PROFILE_SESSION_SAMPLE_RATE=0.5,
)
def test_with_a_dsn_it_initialises_without_personal_data():
    with mock.patch("sentry_sdk.init") as init:
        assert observability.init_sentry() is True
    kwargs = init.call_args.kwargs
    assert kwargs["dsn"] == "https://key@o1.ingest.de.sentry.io/2"
    assert kwargs["environment"] == "beta"
    assert kwargs["traces_sample_rate"] == 0.25
    assert kwargs["profile_session_sample_rate"] == 0.5
    assert kwargs["profile_lifecycle"] == "trace"
    assert kwargs["send_default_pii"] is False
    assert kwargs["enable_logs"] is True
    assert kwargs["before_send"] is observability.before_send


def test_an_event_carries_the_request_workspace_and_user_ids():
    tokens = (request_id_var.set("req-1"), workspace_id_var.set("7"), user_id_var.set("9"))
    try:
        event = observability.before_send({"tags": {"existing": "x"}}, {})
    finally:
        for var, token in zip((request_id_var, workspace_id_var, user_id_var), tokens, strict=True):
            var.reset(token)
    assert event["tags"] == {"existing": "x", "request_id": "req-1", "workspace_id": "7"}
    assert event["user"] == {"id": "9"}


def test_an_event_outside_a_request_is_left_alone():
    assert observability.before_send({"message": "m"}, {}) == {"message": "m"}


@override_settings(SENTRY_DSN="")
def test_the_check_command_refuses_without_a_dsn():
    with pytest.raises(CommandError, match="SENTRY_DSN"):
        call_command("sentry_check")


@override_settings(SENTRY_DSN="https://key@o1.ingest.de.sentry.io/2")
def test_the_check_command_sends_an_error_and_a_log():
    with (
        mock.patch("common.observability.init_sentry", return_value=True),
        mock.patch("sentry_sdk.capture_exception", return_value="evt-1") as capture,
        mock.patch("sentry_sdk.logger.error") as log,
        mock.patch("sentry_sdk.flush") as flush,
    ):
        call_command("sentry_check")
    assert isinstance(capture.call_args.args[0], ZeroDivisionError)
    log.assert_called_once()
    flush.assert_called_once()
