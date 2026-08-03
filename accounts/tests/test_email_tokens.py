"""EmailToken and the verify / reset flows."""

from __future__ import annotations

import datetime as dt

import pytest
import time_machine
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient

from accounts.models import EmailToken, EmailTokenPurpose

pytestmark = pytest.mark.django_db

User = get_user_model()
PASSWORD = "correct-horse-battery-staple"

VERIFY_URL = "/api/v1/auth/verify-email/"
RESEND_URL = "/api/v1/auth/resend-verify/"
RESET_URL = "/api/v1/auth/password/reset/"
RESET_CONFIRM_URL = "/api/v1/auth/password/reset/confirm/"


@pytest.fixture
def user():
    return User.objects.create_user(email="jordan@example.com", password=PASSWORD)


def test_email_token_single_use_and_expiring(user) -> None:
    _, raw = EmailToken.issue(user, EmailTokenPurpose.VERIFY)

    # First use succeeds.
    assert EmailToken.consume(raw, EmailTokenPurpose.VERIFY) is not None
    # Replay does not.
    assert EmailToken.consume(raw, EmailTokenPurpose.VERIFY) is None

    _, fresh = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    with time_machine.travel(dt.datetime.now(dt.UTC) + dt.timedelta(days=4)):
        assert EmailToken.consume(fresh, EmailTokenPurpose.VERIFY) is None


def test_token_is_stored_only_as_a_hash(user) -> None:
    """A database leak must not be replayable into account takeover."""
    _, raw = EmailToken.issue(user, EmailTokenPurpose.RESET)

    with connection.cursor() as cursor:
        cursor.execute("SELECT token_hash FROM accounts_emailtoken")
        stored = cursor.fetchone()[0]

    assert raw not in stored
    assert stored == EmailToken.hash_token(raw)
    assert len(stored) == 64


def test_a_token_cannot_be_used_for_another_purpose(user) -> None:
    """Otherwise a verification link — the lower-value, longer-lived one —
    could be redeemed as a password reset."""
    _, raw = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    assert EmailToken.consume(raw, EmailTokenPurpose.RESET) is None


def test_issuing_supersedes_the_previous_token(user) -> None:
    """A resend must kill the earlier link: the first may have gone to a
    mistyped address."""
    _, first = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    _, second = EmailToken.issue(user, EmailTokenPurpose.VERIFY)

    assert EmailToken.consume(first, EmailTokenPurpose.VERIFY) is None
    assert EmailToken.consume(second, EmailTokenPurpose.VERIFY) is not None


def test_reset_tokens_expire_faster_than_verification_tokens(user) -> None:
    verify, _ = EmailToken.issue(user, EmailTokenPurpose.VERIFY)
    reset, _ = EmailToken.issue(user, EmailTokenPurpose.RESET)
    assert reset.expires_at < verify.expires_at


# --- endpoints -----------------------------------------------------------
def test_verify_email_marks_the_user_verified(user) -> None:
    _, raw = EmailToken.issue(user, EmailTokenPurpose.VERIFY)

    response = APIClient().post(VERIFY_URL, {"token": raw}, format="json")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.is_email_verified is True


def test_verify_email_rejects_a_bad_token() -> None:
    response = APIClient().post(VERIFY_URL, {"token": "nonsense"}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_token"


def test_resend_verification_queues_another_email(user, outbox) -> None:
    client = APIClient()
    client.force_authenticate(user)

    assert client.post(RESEND_URL).status_code == 202
    assert len(outbox) == 1


def test_resend_is_a_no_op_once_verified(user, outbox) -> None:
    """Otherwise it is a free way to have us mail someone repeatedly."""
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])

    client = APIClient()
    client.force_authenticate(user)

    assert client.post(RESEND_URL).status_code == 202
    assert outbox == []


def test_password_reset_does_not_reveal_whether_an_account_exists(user, outbox) -> None:
    known = APIClient().post(RESET_URL, {"email": user.email}, format="json")
    unknown = APIClient().post(RESET_URL, {"email": "nobody@example.com"}, format="json")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    # ...but only the real address is actually mailed.
    assert [email.to for email in outbox] == [user.email]


def test_password_reset_confirm_changes_the_password(user, outbox) -> None:
    APIClient().post(RESET_URL, {"email": user.email}, format="json")
    raw = outbox[0].context["reset_url"].split("token=")[1]

    response = APIClient().post(
        RESET_CONFIRM_URL, {"token": raw, "password": "a-brand-new-secret-99"}, format="json"
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password("a-brand-new-secret-99")


def test_completing_a_reset_also_verifies_the_address(user, outbox) -> None:
    """Redeeming a link from the inbox proves control of the address."""
    assert user.is_email_verified is False
    APIClient().post(RESET_URL, {"email": user.email}, format="json")
    raw = outbox[0].context["reset_url"].split("token=")[1]

    APIClient().post(
        RESET_CONFIRM_URL, {"token": raw, "password": "a-brand-new-secret-99"}, format="json"
    )

    user.refresh_from_db()
    assert user.is_email_verified is True


def test_reset_confirm_rejects_a_weak_password(user, outbox) -> None:
    APIClient().post(RESET_URL, {"email": user.email}, format="json")
    raw = outbox[0].context["reset_url"].split("token=")[1]

    response = APIClient().post(
        RESET_CONFIRM_URL, {"token": raw, "password": "1234"}, format="json"
    )

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.check_password(PASSWORD)
