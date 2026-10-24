"""Captcha on every form anyone can post to (`common.captcha`).

The fake passes every token but `fail`, so these tests reach the refusal on
each endpoint without Cloudflare, and the Turnstile adapter is tested on its
own against a stubbed `siteverify`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from rest_framework.test import APIClient

from accounts.models import User
from common import captcha
from common.checks import check_no_fakes_in_deploy
from tests.httpx_transport import patch_httpx_transport

pytestmark = pytest.mark.django_db

API = "/api/v1/"
#: Public token actions: a refused captcha is a 400 before the link is even
#: looked up; a passing one reaches the lookup, which 404s the made-up token.
TOKEN_ACTIONS = [
    "reminders/nope/confirm/",
    "reminders/nope/snooze/",
    "reminders/nope/skip/",
    "review/nope/approve/",
    "review/nope/comment/",
    "review/nope/request-changes/",
]


@pytest.fixture(autouse=True)
def _clear_override() -> Any:
    yield
    captcha.set_override(None)


def test_registration_is_refused_without_a_passing_captcha() -> None:
    response = APIClient().post(
        f"{API}auth/register/",
        {"email": "bot@example.com", "password": "Sturdy-pass-91", "captcha": "fail"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_failed"
    assert not User.objects.filter(email="bot@example.com").exists()


def test_registration_goes_through_with_a_passing_captcha() -> None:
    response = APIClient().post(
        f"{API}auth/register/",
        {"email": "human@example.com", "password": "Sturdy-pass-91", "captcha": "ok"},
        format="json",
    )

    assert response.status_code == 201, response.json()


def test_login_is_refused_without_a_passing_captcha(user: Any) -> None:
    response = APIClient().post(
        f"{API}auth/login/",
        {"email": user.email, "password": "irrelevant", "captcha": "fail"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_failed"


def test_a_password_reset_request_is_refused_without_a_passing_captcha(user: Any) -> None:
    response = APIClient().post(
        f"{API}auth/password/reset/", {"email": user.email, "captcha": "fail"}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_failed"


@pytest.mark.parametrize("path", TOKEN_ACTIONS)
def test_a_public_token_action_checks_the_captcha_first(path: str) -> None:
    client = APIClient()

    refused = client.post(f"{API}{path}", {"captcha": "fail"}, format="json")
    passed = client.post(f"{API}{path}", {"captcha": "ok"}, format="json")

    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "captcha_failed"
    assert passed.status_code == 404


# --- the Turnstile adapter ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("answer", "expected"), [({"success": True}, True), ({"success": False}, False)]
)
def test_turnstile_answers_what_cloudflare_says(
    monkeypatch: Any, settings: Any, answer: dict[str, Any], expected: bool
) -> None:
    settings.TURNSTILE_SECRET_KEY = "secret"
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json=answer)

    patch_httpx_transport(monkeypatch, handler)

    assert captcha.TurnstileVerifier().verify("token-1") is expected
    assert b"secret=secret" in sent[0] and b"response=token-1" in sent[0]


def test_turnstile_refuses_when_cloudflare_cannot_be_reached(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    patch_httpx_transport(monkeypatch, handler)

    assert captcha.TurnstileVerifier().verify("token-1") is False


def test_turnstile_refuses_an_empty_token_without_asking() -> None:
    assert captcha.TurnstileVerifier().verify("") is False


def test_the_real_verifier_is_used_once_a_secret_is_set(settings: Any) -> None:
    settings.USE_FAKE_CAPTCHA = False
    assert isinstance(captcha.get_captcha_verifier(), captcha.TurnstileVerifier)


def test_a_deployment_without_a_captcha_key_is_warned(settings: Any) -> None:
    settings.USE_FAKE_CAPTCHA = True

    messages = check_no_fakes_in_deploy(None)

    assert "common.W012" in {message.id for message in messages}
    # A warning, not a refusal: the key is an operator step.
    refusals = [message.msg for message in messages if message.id == "common.E010"]
    assert not any("USE_FAKE_CAPTCHA" in msg for msg in refusals)
