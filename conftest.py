"""Shared fixtures.

At the repo root so every app's tests get the same isolation: Redis and the mail
outbox are process-wide, and a leaked key or a stale outbox entry makes another
test fail somewhere unrelated.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

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


@pytest.fixture(autouse=True)
def _isolate_platform_adapter() -> Iterator[None]:
    """The fake publishing adapter is module-level, like the fake mail sender
    and the fake AI providers — so its recorded calls and its idempotency
    memory have to be reset between tests or a replay from one leaks into the
    next."""
    from channels.adapters.fake import _fake_adapter

    _fake_adapter.clear()
    yield
    _fake_adapter.clear()


@pytest.fixture
def platform_adapter() -> Any:
    from channels.adapters.fake import _fake_adapter

    return _fake_adapter


@pytest.fixture
def paid_workspace(workspace: Any, plans: dict[str, Any]) -> Any:
    """Auto-publish is a paid feature (D4), so every connect and publish test
    needs a plan that has it. Pro rather than Advanced: five social accounts
    is the smaller cap of the two, which is what makes the I6 account-cap test
    meaningful without seeding ten accounts first."""
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    return workspace


@pytest.fixture
def social_account(paid_workspace: Any) -> Any:
    from channels.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="instagram",
        handle="@acme",
        display_name="Acme Studio",
        provider_account_id="acct-instagram-1",
    )


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


@pytest.fixture
def workspace(plans: dict[str, Any], user: Any) -> Any:
    """Moved here from billing/tests/conftest.py once content needed it too —
    the same one-copy-per-file collapse that file's own docstring describes."""
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


@pytest.fixture
def make_png_upload() -> Any:
    """Moved here from content/tests/conftest.py once products needed it too."""

    def _make(name: str = "photo.png", size: tuple[int, int] = (600, 600)) -> SimpleUploadedFile:
        buffer = io.BytesIO()
        Image.new("RGB", size, color=(120, 130, 200)).save(buffer, format="PNG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")

    return _make


@pytest.fixture
def generation_costs(db: None) -> None:
    """Moved here from ai/tests/conftest.py once products needed it too."""
    from django.core.management import call_command

    call_command("seed_generation_costs", verbosity=0)


@pytest.fixture(autouse=True)
def _fake_ai_providers(settings: Any) -> None:
    """AI tests use the fake providers by default (A8), mirroring
    `USE_FAKE_BILLING`'s test-time default. Moved here from ai/tests/conftest.py
    once products needed it too."""
    settings.USE_FAKE_AI_PROVIDERS = True


@pytest.fixture(autouse=True)
def _clear_fake_providers() -> Iterator[None]:
    from ai.providers.fake import _fake_image_provider, _fake_text_provider

    _fake_text_provider.clear()
    _fake_image_provider.clear()
    yield
    _fake_text_provider.clear()
    _fake_image_provider.clear()


# --- collaboration & roles (design.md §8.8) -----------------------------------
# Moved here from workspaces/tests/conftest.py once content's approval-endpoint
# tests needed them too.
@pytest.fixture
def advanced_workspace(workspace: Any, plans: dict[str, Any]) -> Any:
    """Advanced, with the collaboration workflow switched on. §4.1: only
    Advanced has `approval_workflow`."""
    workspace.plan = plans["advanced"]
    workspace.requires_approval = True
    workspace.save(update_fields=["plan", "requires_approval"])
    return workspace


@pytest.fixture
def admin_user(advanced_workspace: Any) -> Any:
    from django.contrib.auth import get_user_model

    from workspaces.models import Membership, Role

    admin = get_user_model().objects.create_user(email="admin@example.com", password=PASSWORD)
    Membership.objects.create(user=admin, workspace=advanced_workspace, role=Role.ADMIN)
    return admin


@pytest.fixture
def contributor_user(advanced_workspace: Any) -> Any:
    from django.contrib.auth import get_user_model

    from workspaces.models import Membership, Role

    contributor = get_user_model().objects.create_user(
        email="contributor@example.com", password=PASSWORD
    )
    Membership.objects.create(user=contributor, workspace=advanced_workspace, role=Role.CONTRIBUTOR)
    return contributor


@pytest.fixture
def viewer_user(advanced_workspace: Any) -> Any:
    from django.contrib.auth import get_user_model

    from workspaces.models import Membership, Role

    viewer = get_user_model().objects.create_user(email="viewer@example.com", password=PASSWORD)
    Membership.objects.create(user=viewer, workspace=advanced_workspace, role=Role.VIEWER)
    return viewer


@pytest.fixture
def advanced_social_account(advanced_workspace: Any) -> Any:
    """The `social_account` fixture above pulls in `paid_workspace`, which
    would set `workspace.plan` back to Pro — a real conflict with
    `advanced_workspace`, since both mutate the same cached `workspace`
    fixture instance. This is `social_account`'s shape, built directly on
    `advanced_workspace` instead."""
    from channels.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=advanced_workspace,
        platform="instagram",
        handle="@acme",
        display_name="Acme Studio",
        provider_account_id="acct-approvals-1",
    )


@pytest.fixture
def client_as() -> Any:
    """`client_as(some_user)` — an `APIClient` authenticated as a user other
    than the root `user`/`auth_client` fixtures', for the multi-actor
    approval tests (a CONTRIBUTOR drafts, an ADMIN decides)."""
    from rest_framework.test import APIClient

    def _make(actor: Any) -> APIClient:
        api = APIClient()
        api.force_authenticate(actor)
        return api

    return _make
