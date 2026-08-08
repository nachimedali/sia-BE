"""Registration (implementation.md Phase 2.3)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from conftest import PASSWORD
from workspaces.models import Membership, Role, Workspace

pytestmark = pytest.mark.django_db

User = get_user_model()
REGISTER_URL = "/api/v1/auth/register/"


def register(client: APIClient | None = None, **overrides):
    payload = {"email": "jordan@example.com", "password": PASSWORD, **overrides}
    return (client or APIClient()).post(REGISTER_URL, payload, format="json")


def test_register_creates_workspace_membership_and_free_plan(plans, outbox) -> None:
    response = register()
    assert response.status_code == 201

    user = User.objects.get(email="jordan@example.com")
    assert user.is_email_verified is False

    workspace = Workspace.objects.get(owner=user)
    assert workspace.plan is not None
    assert workspace.plan.code == "free"
    assert workspace.onboarding_complete is False
    assert workspace.slug

    membership = Membership.objects.get(user=user, workspace=workspace)
    assert membership.role == Role.OWNER


def test_register_returns_a_session_so_the_wizard_can_start(plans) -> None:
    body = register().json()

    # The user is signed in but unverified; step 1 is what gates progress.
    assert body["access"] and body["refresh"]
    assert body["user"]["email"] == "jordan@example.com"
    assert body["user"]["is_email_verified"] is False


def test_register_queues_a_verification_email(
    plans, outbox, django_capture_on_commit_callbacks
) -> None:
    # The email is queued in transaction.on_commit, so a user can never receive
    # a link to an account whose creation later rolled back. Tests run inside a
    # transaction that never commits, hence the explicit capture.
    with django_capture_on_commit_callbacks(execute=True):
        register()

    assert len(outbox) == 1
    email = outbox[0]
    assert email.to == "jordan@example.com"
    assert email.template == "verify_email"
    # The link points at the frontend, not the API.
    assert "/verify-email?token=" in email.context["verify_url"]


def test_email_is_normalised_to_lowercase(plans) -> None:
    register(email="Jordan@Example.COM")
    assert User.objects.filter(email="jordan@example.com").exists()


def test_duplicate_email_is_rejected(plans) -> None:
    register()
    response = register()

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert User.objects.filter(email="jordan@example.com").count() == 1


def test_duplicate_email_is_case_insensitive(plans) -> None:
    register()
    assert register(email="JORDAN@example.com").status_code == 400


def test_weak_passwords_are_rejected(plans) -> None:
    response = register(password="password")

    assert response.status_code == 400
    assert "fields" in response.json()["error"]["detail"]
    assert not User.objects.filter(email="jordan@example.com").exists()


def test_failed_registration_leaves_nothing_behind(plans, outbox) -> None:
    """The whole thing is one transaction: a user without a workspace, or a
    workspace without an owner membership, is an account every later request
    would have to defend against."""
    register(password="123")

    assert User.objects.count() == 0
    assert Workspace.objects.count() == 0
    assert Membership.objects.count() == 0
    assert outbox == []


def test_custom_workspace_name_is_used(plans) -> None:
    register(workspace_name="Acme Studio")
    assert Workspace.objects.get().name == "Acme Studio"


def test_workspace_name_defaults_from_the_email(plans) -> None:
    register(email="jordan.diaz@example.com")
    assert Workspace.objects.get().name == "Jordan Diaz Workspace"


def test_workspace_slugs_do_not_collide(plans) -> None:
    register(email="a@example.com", workspace_name="Acme Studio")
    register(email="b@example.com", workspace_name="Acme Studio")

    slugs = set(Workspace.objects.values_list("slug", flat=True))
    assert len(slugs) == 2


def test_every_workspace_gets_a_referral_code(plans) -> None:
    """Affiliates are deferred (design.md §12), but the code ships in v1 so
    attribution can be backfilled without migrating live rows."""
    register(email="a@example.com")
    register(email="b@example.com")

    codes = set(Workspace.objects.values_list("referral_code", flat=True))
    assert len(codes) == 2
    assert all(codes)
