"""Shared fixtures.

At the repo root so every app's tests get the same isolation: Redis and the mail
outbox are process-wide, and a leaked key or a stale outbox entry makes another
test fail somewhere unrelated.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from common.mail import _fake_sender
from common.redis import get_redis

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _isolate_redis() -> Iterator[None]:
    """Tests run against real Redis (design.md A18), on DB 15."""
    client = get_redis()
    client.flushdb()
    yield
    client.flushdb()


@pytest.fixture(autouse=True)
def _isolate_mail() -> Iterator[None]:
    _fake_sender.clear()
    yield
    _fake_sender.clear()


@pytest.fixture
def outbox() -> list[Any]:
    """The FakeMailSender outbox (design.md §9, A8)."""
    return _fake_sender.outbox


@pytest.fixture
def plans(db: None) -> dict[str, Any]:
    from django.core.management import call_command

    call_command("seed_plans", verbosity=0)
    from billing.models import Plan

    return {plan.code: plan for plan in Plan.objects.all()}


@pytest.fixture
def category(db: None) -> Any:
    from categories.models import Category

    return Category.objects.create(name="Homeware & Ceramics", slug="homeware-ceramics")


@pytest.fixture
def user(db: None) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="jordan@example.com", password=PASSWORD)


@pytest.fixture
def auth_client(user: Any) -> Any:
    """An APIClient carrying `user`'s identity, bypassing the login endpoint."""
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user)
    return api
