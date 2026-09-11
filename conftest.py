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
from workspaces.services import approvals as approvals_service

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


@pytest.fixture(autouse=True)
def _isolate_media_editor() -> Iterator[None]:
    """Same reasoning as the publish adapter above: the fake editor is
    module-level so a test can inspect its calls after a view has run, which
    makes its record process-wide state (P1-12)."""
    from content.editing.fake import _fake_editor

    _fake_editor.clear()
    yield
    _fake_editor.clear()


@pytest.fixture
def media_editor() -> Any:
    from content.editing.fake import _fake_editor

    return _fake_editor


@pytest.fixture
def platform_adapter() -> Any:
    from channels.adapters.fake import _fake_adapter

    return _fake_adapter


@pytest.fixture(autouse=True)
def _reset_gateway() -> Iterator[None]:
    """The fake billing gateway is module-level so a test can inspect calls
    after a view has run, which makes its call log process-wide state the way
    Redis is."""
    from billing.gateways.fake import _fake_gateway

    _fake_gateway.clear()
    yield
    _fake_gateway.clear()


@pytest.fixture(autouse=True)
def _isolate_push_transport() -> Iterator[None]:
    """The fake push transport is module-level so a test can inspect what a
    view sent after it has run — which makes its record process-wide state, the
    same as the mail outbox and the publish adapter (P2-12)."""
    from notifications.transports import _fake_push

    _fake_push.clear()
    yield
    _fake_push.clear()


@pytest.fixture
def push_transport() -> Any:
    from notifications.transports import _fake_push

    return _fake_push


@pytest.fixture(autouse=True)
def _isolate_metrics_provider() -> Iterator[None]:
    """Same reasoning as the publish adapter above, for the measurement port
    (P0-27). Separate fixture because they are separate ports — a single reset
    covering both would be the first crack in the separation P0-28 asserts."""
    from analytics.providers.fake import fake_provider

    fake_provider().reset()
    yield
    fake_provider().reset()


@pytest.fixture
def metrics_provider() -> Any:
    from analytics.providers.fake import fake_provider

    return fake_provider()


@pytest.fixture
def organization(workspace: Any) -> Any:
    """The org `provision_workspace` created alongside the workspace (P0-45).

    Derived rather than constructed, so a test can never assert against an
    organization the application would not have made.
    """
    return workspace.organization


@pytest.fixture
def paid_workspace(workspace: Any, plans: dict[str, Any]) -> Any:
    """Auto-publish is a paid feature (D4), so every connect and publish test
    needs a plan that has it. Pro rather than Advanced: five social accounts
    is the smaller cap of the two, which is what makes the I6 account-cap test
    meaningful without seeding ten accounts first.

    The plan is set on the **organization** — since P0-56 that is the only
    place it lives (L-1: billing pools at the company)."""
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])
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
def other_user(db: None) -> Any:
    """A second account with no workspace of its own until a test gives it one.
    Every cross-tenant assertion needs one, and four files were making it by
    hand."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="sam@example.com", password=PASSWORD)


@pytest.fixture
def auth_client(user: Any) -> Any:
    """An APIClient carrying `user`'s identity, bypassing the login endpoint."""
    from rest_framework.test import APIClient

    api = APIClient()
    api.force_authenticate(user)
    return api


@pytest.fixture
def plans_by_code(plans: dict[str, Any]) -> dict[str, Any]:
    """Alias for `plans`, for modules that also import `billing.services.plans`
    and would otherwise shadow the module with the fixture."""
    return plans


@pytest.fixture
def seeded_plans(plans: dict[str, Any]) -> dict[str, Any]:
    """Alias for `plans`, for the handful of modules that also import the
    `billing.services.plans` module and would otherwise shadow it."""
    return plans


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
    """Advanced, with a **blocking** approval chain.

    `requires_approval` was a column until P2-04; it is now
    `ApprovalChain.blocks_publish` on the workspace's default chain, which is
    the only thing that can also say how many stages and who. Advanced is what
    the plan buys — chain *depth* (P2-13) — not approval itself, which C-02
    makes universal.
    """
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    chain = approvals_service.default_chain(workspace)
    chain.blocks_publish = True
    chain.save(update_fields=["blocks_publish"])
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
    would set the organization's plan back to Pro — a real conflict with
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


@pytest.fixture
def media_asset(workspace: Any, make_png_upload: Any) -> Any:
    """Moved here from content/tests/conftest.py once ai/ needed it too."""
    from content.services.media import ingest_media

    return ingest_media(workspace=workspace, upload=make_png_upload())


@pytest.fixture
def text_provider() -> Any:
    """The module-level fake text provider, for asserting on what a generation
    actually asked the vendor for — a vision call that never opened the file
    would otherwise pass every test above it."""
    from ai.providers.fake import _fake_text_provider

    return _fake_text_provider


@pytest.fixture
def trend_corpus(category: Any, db: None) -> Any:
    """A small category corpus with hashtags in it (P1-13).

    Dates are **relative to now**, never absolute: P0-63 was a whole day lost
    to a fixture whose dates fell out of the 14-day window in August, and the
    fix was to make that class of bug impossible rather than to move the dates.
    """
    import datetime as dt

    from django.utils import timezone

    from trends.models import TrendItem, TrendSource

    source = TrendSource.objects.create(
        category=category, platform="instagram", kind="REDDIT", vendor="fake"
    )
    bodies = [
        "New glaze day #ceramics #handmade #studio",
        "Throwing mugs all morning #ceramics #handmade",
        "Kiln unloading #ceramics",
        # Says it four times: counted once, because a ranking that counted
        # mentions would let one spammy caption dominate a whole vertical.
        "#studio #studio #studio #studio",
    ]
    for index, body in enumerate(bodies):
        TrendItem.objects.create(
            source=source,
            external_id=f"item-{index}",
            body=body,
            posted_at=timezone.now() - dt.timedelta(days=index + 1),
        )
    return source
