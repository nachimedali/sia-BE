"""Verification by six-digit code, beside the emailed link.

The code is what onboarding step 1 asks for; the link stays as a fallback for
someone who opens the mail on another device. Both prove control of the
address, and the first one to do so is what sends the welcome email.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from rest_framework.test import APIClient

from accounts.models import EmailToken, EmailTokenPurpose
from accounts.services import auth as auth_service
from billing.models import FeatureFlag
from billing.services.flags import TRANSACTIONAL_EMAIL

pytestmark = pytest.mark.django_db

REGISTER_URL = "/api/v1/auth/register/"
VERIFY_CODE_URL = "/api/v1/auth/verify-code/"
VERIFY_LINK_URL = "/api/v1/auth/verify-email/"
RESEND_URL = "/api/v1/auth/resend-verify/"
PASSWORD = "correct-horse-battery-staple"


def _register(client: APIClient, email: str = "ines@example.com") -> dict[str, Any]:
    response = client.post(REGISTER_URL, {"email": email, "password": PASSWORD}, format="json")
    assert response.status_code == 201
    return response.json()


def _signed_in(email: str = "ines@example.com") -> tuple[APIClient, Any]:
    from django.contrib.auth import get_user_model

    client = APIClient()
    _register(client, email)
    user = get_user_model().objects.get(email=email)
    client.force_authenticate(user)
    return client, user


def _verification(outbox: list[Any], to: str | None = None) -> Any:
    # `verify_email_link` is the link-only email the flag-off path sends.
    return next(
        e
        for e in outbox
        if e.template in {"verify_email", "verify_email_link"} and to in {None, e.to}
    )


def _welcomes(outbox: list[Any]) -> list[Any]:
    return [e for e in outbox if e.template == "welcome"]


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


@pytest.fixture
def registered(
    plans: dict[str, Any], outbox: list[Any], django_capture_on_commit_callbacks: Any
) -> tuple[APIClient, Any, str]:
    with django_capture_on_commit_callbacks(execute=True):
        client, user = _signed_in()
    return client, user, _verification(outbox).context["code"]


# -----------------------------------------------------------------------------
# The email
# -----------------------------------------------------------------------------
def test_verification_email_carries_a_six_digit_code_and_the_link(
    registered: tuple[APIClient, Any, str], outbox: list[Any]
) -> None:
    _, _, code = registered
    email = _verification(outbox)

    assert len(code) == 6 and code.isdigit()
    assert "/verify-email?token=" in email.context["verify_url"]
    assert email.context["expires_minutes"] == 15
    assert code in email.subject


def test_the_code_is_stored_hashed(registered: tuple[APIClient, Any, str]) -> None:
    _, user, code = registered
    token = EmailToken.objects.get(user=user, purpose=EmailTokenPurpose.VERIFY_CODE)

    assert code not in token.token_hash


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------
def test_the_right_code_verifies_the_address(registered: tuple[APIClient, Any, str]) -> None:
    client, user, code = registered

    response = client.post(VERIFY_CODE_URL, {"code": code}, format="json")

    assert response.status_code == 200
    assert response.json() == {"verified": True}
    user.refresh_from_db()
    assert user.is_email_verified


def test_a_code_cannot_be_used_twice(registered: tuple[APIClient, Any, str]) -> None:
    from django.contrib.auth import get_user_model

    client, user, code = registered
    assert client.post(VERIFY_CODE_URL, {"code": code}, format="json").status_code == 200
    get_user_model().objects.filter(pk=user.pk).update(is_email_verified=False)
    user.refresh_from_db()
    client.force_authenticate(user)

    response = client.post(VERIFY_CODE_URL, {"code": code}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"


def test_an_already_verified_account_is_answered_verified(
    registered: tuple[APIClient, Any, str],
) -> None:
    client, user, _ = registered
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])

    response = client.post(VERIFY_CODE_URL, {"code": "000000"}, format="json")

    assert response.status_code == 200


# -----------------------------------------------------------------------------
# Every refusal
# -----------------------------------------------------------------------------
def test_a_wrong_code_is_refused(registered: tuple[APIClient, Any, str]) -> None:
    client, user, code = registered

    response = client.post(VERIFY_CODE_URL, {"code": _wrong(code)}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"
    user.refresh_from_db()
    assert not user.is_email_verified


@pytest.mark.parametrize("value", ["", "12345", "1234567", "abcdef", "12 456"])
def test_a_malformed_code_is_a_validation_error(
    registered: tuple[APIClient, Any, str], value: str
) -> None:
    client, _, _ = registered

    response = client.post(VERIFY_CODE_URL, {"code": value}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_an_expired_code_is_refused(registered: tuple[APIClient, Any, str]) -> None:
    client, _, code = registered

    with time_machine.travel(dt.timedelta(minutes=16)):
        response = client.post(VERIFY_CODE_URL, {"code": code}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"


def test_five_wrong_tries_burn_the_code(registered: tuple[APIClient, Any, str]) -> None:
    """Six digits is a million guesses; five per code makes walking them
    pointless even before the throttle."""
    client, _, code = registered
    for _ in range(5):
        client.post(VERIFY_CODE_URL, {"code": _wrong(code)}, format="json")

    response = client.post(VERIFY_CODE_URL, {"code": code}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_code"


def test_another_users_code_does_not_verify_me(
    plans: dict[str, Any], outbox: list[Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        mine, me = _signed_in("me@example.com")
        _signed_in("them@example.com")
    theirs = _verification(outbox, to="them@example.com").context["code"]
    my_code = _verification(outbox, to="me@example.com").context["code"]
    if theirs == my_code:  # one in a million, and not what this test is about
        pytest.skip("both accounts drew the same code")

    response = mine.post(VERIFY_CODE_URL, {"code": theirs}, format="json")

    assert response.status_code == 400
    me.refresh_from_db()
    assert not me.is_email_verified


def test_verifying_by_code_needs_a_session(plans: dict[str, Any]) -> None:
    response = APIClient().post(VERIFY_CODE_URL, {"code": "123456"}, format="json")

    assert response.status_code == 401


def test_resend_replaces_the_code(
    registered: tuple[APIClient, Any, str],
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    client, _, first = registered
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post(RESEND_URL, format="json").status_code == 202
    second = [e for e in outbox if e.template == "verify_email"][-1].context["code"]

    if first != second:
        stale = client.post(VERIFY_CODE_URL, {"code": first}, format="json")
        assert stale.status_code == 400
    assert client.post(VERIFY_CODE_URL, {"code": second}, format="json").status_code == 200


# -----------------------------------------------------------------------------
# Welcome — once, on the first proof of the address
# -----------------------------------------------------------------------------
def test_verifying_by_code_sends_the_welcome_email_once(
    registered: tuple[APIClient, Any, str],
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    client, user, code = registered
    assert _welcomes(outbox) == []  # never at registration

    with django_capture_on_commit_callbacks(execute=True):
        client.post(VERIFY_CODE_URL, {"code": code}, format="json")
        client.post(VERIFY_CODE_URL, {"code": code}, format="json")

    welcomes = _welcomes(outbox)
    assert len(welcomes) == 1
    assert welcomes[0].to == user.email
    context = welcomes[0].context
    assert context["workspace_name"]
    assert context["plan_name"]


def test_verifying_by_link_also_sends_the_welcome_email(
    plans: dict[str, Any], outbox: list[Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _signed_in()
    token = _verification(outbox).context["verify_url"].split("token=", 1)[1]

    with django_capture_on_commit_callbacks(execute=True):
        assert APIClient().post(VERIFY_LINK_URL, {"token": token}, format="json").status_code == 200

    assert len(_welcomes(outbox)) == 1


def test_a_password_reset_that_verifies_does_not_send_a_welcome(
    registered: tuple[APIClient, Any, str],
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    """The welcome belongs to the sign-up journey; someone resetting a password
    on an account they never confirmed is not starting one."""
    _, user, _ = registered
    _, raw = EmailToken.issue(user, EmailTokenPurpose.RESET)

    with django_capture_on_commit_callbacks(execute=True):
        assert auth_service.confirm_password_reset(raw, "another-long-passphrase")

    assert _welcomes(outbox) == []


# -----------------------------------------------------------------------------
# Flag off — pre-change behaviour: link only, no code, no welcome
# -----------------------------------------------------------------------------
@pytest.fixture
def flag_off(db: None) -> None:
    FeatureFlag.objects.create(organization=None, key=TRANSACTIONAL_EMAIL, enabled=False)


def test_flag_off_sends_the_link_without_a_code(
    flag_off: None,
    plans: dict[str, Any],
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _signed_in()
    email = _verification(outbox)

    assert "code" not in email.context
    assert "/verify-email?token=" in email.context["verify_url"]


def test_flag_off_hides_the_code_endpoint(
    flag_off: None, plans: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        client, _ = _signed_in()

    response = client.post(VERIFY_CODE_URL, {"code": "123456"}, format="json")

    assert response.status_code == 404


def test_flag_off_sends_no_welcome(
    flag_off: None,
    plans: dict[str, Any],
    outbox: list[Any],
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _signed_in()
    token = _verification(outbox).context["verify_url"].split("token=", 1)[1]

    with django_capture_on_commit_callbacks(execute=True):
        APIClient().post(VERIFY_LINK_URL, {"token": token}, format="json")

    assert _welcomes(outbox) == []
