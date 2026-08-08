"""Per-IP auth rate limits (implementation.md Phase 2.2).

The threat is credential stuffing: an attacker replaying a leaked password list.
Buckets live in Redis so the limit holds across every web process.
"""

from __future__ import annotations

import pytest
import time_machine
from rest_framework.test import APIClient

from common.throttling import LoginThrottle, RegisterThrottle
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

LOGIN_URL = "/api/v1/auth/login/"
REGISTER_URL = "/api/v1/auth/register/"
RESET_URL = "/api/v1/auth/password/reset/"


def attempt_login(client: APIClient, password: str = "wrong", ip: str = "203.0.113.9"):
    return client.post(
        LOGIN_URL,
        {"email": "jordan@example.com", "password": password},
        format="json",
        REMOTE_ADDR=ip,
    )


def test_login_rate_limited_after_n_attempts(user) -> None:
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for _ in range(LoginThrottle.capacity):
            assert attempt_login(client).status_code == 401

        blocked = attempt_login(client)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    # Tells the client exactly how long to back off, rather than making it guess.
    assert int(blocked["Retry-After"]) >= 1


def test_the_limit_lifts_as_the_bucket_refills(user) -> None:
    client = APIClient()

    with time_machine.travel(0, tick=False) as traveller:
        for _ in range(LoginThrottle.capacity):
            attempt_login(client)
        assert attempt_login(client).status_code == 429

        traveller.shift(1 / LoginThrottle.refill_per_second)
        # A locked-out legitimate user must be able to get back in.
        assert attempt_login(client, password=PASSWORD).status_code == 200


def test_the_limit_is_per_ip_not_global(user) -> None:
    """One attacker must not be able to lock out every other user."""
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for _ in range(LoginThrottle.capacity + 1):
            attempt_login(client, ip="203.0.113.9")

        other = attempt_login(client, password=PASSWORD, ip="198.51.100.4")

    assert other.status_code == 200


def test_a_correct_password_still_costs_a_token(user) -> None:
    """Otherwise an attacker who lands one valid credential gets a free run at
    the rest of the list."""
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for _ in range(LoginThrottle.capacity):
            assert attempt_login(client, password=PASSWORD).status_code == 200
        assert attempt_login(client, password=PASSWORD).status_code == 429


def test_registration_is_rate_limited(plans) -> None:
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for index in range(RegisterThrottle.capacity):
            response = client.post(
                REGISTER_URL,
                {"email": f"user{index}@example.com", "password": PASSWORD},
                format="json",
                REMOTE_ADDR="203.0.113.9",
            )
            assert response.status_code == 201

        blocked = client.post(
            REGISTER_URL,
            {"email": "one-too-many@example.com", "password": PASSWORD},
            format="json",
            REMOTE_ADDR="203.0.113.9",
        )

    assert blocked.status_code == 429


def test_password_reset_is_rate_limited(user) -> None:
    """This one sends mail to an address the requester may not own, so it is an
    outbound-spam control as much as an auth control."""
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for _ in range(5):
            assert (
                client.post(
                    RESET_URL, {"email": user.email}, format="json", REMOTE_ADDR="203.0.113.9"
                ).status_code
                == 202
            )

        blocked = client.post(
            RESET_URL, {"email": user.email}, format="json", REMOTE_ADDR="203.0.113.9"
        )

    assert blocked.status_code == 429


def test_forwarded_ip_is_honoured_behind_a_proxy(user) -> None:
    client = APIClient()

    with time_machine.travel(0, tick=False):
        for _ in range(LoginThrottle.capacity + 1):
            client.post(
                LOGIN_URL,
                {"email": user.email, "password": "wrong"},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1",
                REMOTE_ADDR="10.0.0.1",
            )

        # Same proxy, different client: must not inherit the block.
        other = client.post(
            LOGIN_URL,
            {"email": user.email, "password": PASSWORD},
            format="json",
            HTTP_X_FORWARDED_FOR="198.51.100.4, 10.0.0.1",
            REMOTE_ADDR="10.0.0.1",
        )

    assert other.status_code == 200


def test_limits_are_tunable_without_a_deploy(user, settings) -> None:
    """Ops need to loosen a limit during an incident; hardcoding would make
    that a code change."""
    settings.AUTH_THROTTLES = {"auth:login": {"capacity": 2}}
    client = APIClient()

    with time_machine.travel(0, tick=False):
        assert attempt_login(client).status_code == 401
        assert attempt_login(client).status_code == 401
        assert attempt_login(client).status_code == 429
