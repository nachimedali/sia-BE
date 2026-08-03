"""Phase 1 stub auth: JWT issue and refresh against the custom user model."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

User = get_user_model()

LOGIN_URL = "/api/v1/auth/login/"
REFRESH_URL = "/api/v1/auth/refresh/"
LOGOUT_URL = "/api/v1/auth/logout/"

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def user():
    return User.objects.create_user(email="jordan@example.com", password=PASSWORD)


def test_email_is_the_username_field() -> None:
    assert User.USERNAME_FIELD == "email"
    assert User.REQUIRED_FIELDS == []


def test_login_issues_an_access_and_refresh_pair(user) -> None:
    response = APIClient().post(
        LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json"
    )

    assert response.status_code == 200
    assert set(response.json()) == {"access", "refresh"}


def test_refresh_returns_a_new_access_token(user) -> None:
    client = APIClient()
    refresh = client.post(
        LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json"
    ).json()["refresh"]

    response = client.post(REFRESH_URL, {"refresh": refresh}, format="json")

    assert response.status_code == 200
    assert response.json()["access"]


def test_rotated_refresh_token_is_blacklisted(user) -> None:
    """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION: a stolen refresh token
    stops working the moment the legitimate holder uses it."""
    client = APIClient()
    original = client.post(
        LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json"
    ).json()["refresh"]

    assert client.post(REFRESH_URL, {"refresh": original}, format="json").status_code == 200

    replayed = client.post(REFRESH_URL, {"refresh": original}, format="json")
    assert replayed.status_code == 401


def test_bad_credentials_use_the_error_envelope(user) -> None:
    response = APIClient().post(
        LOGIN_URL, {"email": user.email, "password": "wrong"}, format="json"
    )

    assert response.status_code == 401
    error = response.json()["error"]
    assert set(error) <= {"code", "message", "detail", "upgrade"}
    assert error["message"]


def test_unknown_email_is_indistinguishable_from_a_wrong_password(user) -> None:
    """Otherwise the endpoint is an account-enumeration oracle."""
    unknown = APIClient().post(
        LOGIN_URL, {"email": "nobody@example.com", "password": PASSWORD}, format="json"
    )
    wrong = APIClient().post(LOGIN_URL, {"email": user.email, "password": "wrong"}, format="json")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_logout_blacklists_the_refresh_token(user) -> None:
    client = APIClient()
    refresh = client.post(
        LOGIN_URL, {"email": user.email, "password": PASSWORD}, format="json"
    ).json()["refresh"]

    assert client.post(LOGOUT_URL, {"refresh": refresh}, format="json").status_code == 200
    assert client.post(REFRESH_URL, {"refresh": refresh}, format="json").status_code == 401


def test_superuser_creation_requires_flags() -> None:
    with pytest.raises(ValueError, match="is_staff=True"):
        User.objects.create_superuser("a@example.com", PASSWORD, is_staff=False)
    with pytest.raises(ValueError, match="is_superuser=True"):
        User.objects.create_superuser("b@example.com", PASSWORD, is_superuser=False)


def test_user_requires_an_email() -> None:
    with pytest.raises(ValueError, match="must have an email address"):
        User.objects.create_user(email="", password=PASSWORD)
