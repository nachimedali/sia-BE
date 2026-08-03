"""Settings modules.

Importing prod here is the point: a typo or a missing env key in prod.py would
otherwise only surface during a deploy.
"""

from __future__ import annotations

import importlib

import pytest

REQUIRED_PROD_ENV = {
    "SECRET_KEY": "test-secret",
    "FIELD_ENCRYPTION_KEY": "GRIfoAFcw-BX4tX-C7YSHhBQZkm2wJ5V2fj8HAJz3xk=",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "EMAIL_HOST": "smtp.example.com",
    "EMAIL_HOST_USER": "mailer",
    "EMAIL_HOST_PASSWORD": "secret",
    "DEFAULT_FROM_EMAIL": "no-reply@example.com",
}


@pytest.fixture
def prod_settings(monkeypatch):
    for key, value in REQUIRED_PROD_ENV.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(importlib.import_module("config.settings.prod"))


def test_prod_settings_import_cleanly(prod_settings) -> None:
    assert prod_settings.DEBUG is False


def test_prod_hardens_transport_and_cookies(prod_settings) -> None:
    assert prod_settings.SECURE_SSL_REDIRECT is True
    assert prod_settings.SESSION_COOKIE_SECURE is True
    assert prod_settings.CSRF_COOKIE_SECURE is True
    assert prod_settings.SECURE_HSTS_SECONDS >= 31536000
    assert prod_settings.X_FRAME_OPTIONS == "DENY"


def test_prod_does_not_expose_the_browsable_api(prod_settings) -> None:
    """The browsable renderer is a dev convenience; shipping it exposes an
    interactive console against production data."""
    assert prod_settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] == [
        "rest_framework.renderers.JSONRenderer"
    ]


def test_dev_settings_import_cleanly() -> None:
    dev = importlib.import_module("config.settings.dev")
    assert dev.DEBUG is True
