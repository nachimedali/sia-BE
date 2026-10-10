"""Explicit workspace context and the permission set (P0-11, P0-12, P0-14, P0-15).

`active_workspace` is gone. It answered "the caller's oldest membership", which
is unambiguous only while everyone has exactly one workspace — the moment a
second exists it silently routes a request at the wrong tenant's data, and
nothing in the request says which one was meant. These tests pin the
replacement's three answers: named, unambiguous, or refused.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.db.utils import IntegrityError
from django.urls import reverse

from common import workspaces as workspace_context
from common.exceptions import NotFoundError, OCCSError
from workspaces.models import Membership, Organization, OrganizationMembership, Role
from workspaces.permissions import caller_permissions
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db


def _Request(user: Any, workspace_id: Any = None) -> Any:  # noqa: N802 — reads as a class
    """The two attributes the resolver reads. A DRF `Request` would drag a
    view, a parser and an authenticator in for no extra coverage, and typing
    the stub as `Any` keeps mypy from insisting on the real thing for a
    duck-typed pair of attributes."""

    return SimpleNamespace(
        user=user,
        META={} if workspace_id is None else {"HTTP_X_WORKSPACE_ID": str(workspace_id)},
    )


# -----------------------------------------------------------------------------
# P0-14 — the silent fallback is gone
# -----------------------------------------------------------------------------
def test_active_workspace_no_longer_exists() -> None:
    """Deleted, not deprecated. A shim would keep the leak reachable."""
    assert not hasattr(workspace_context, "active_workspace")


def test_a_sole_workspace_still_needs_no_header(user: Any, workspace: Any) -> None:
    """One membership has one answer, so requiring the header here would break
    every existing client to prevent an ambiguity that does not exist."""
    assert workspace_context.request_workspace(_Request(user)) == workspace


def test_two_workspaces_without_a_header_is_refused_not_guessed(user: Any, workspace: Any) -> None:
    """The whole of P0-14. Picking the oldest is the cross-tenant leak."""
    second = provision_workspace(user, name="Second Brand")
    assert second != workspace

    with pytest.raises(OCCSError) as caught:
        workspace_context.request_workspace(_Request(user))

    assert caught.value.code == "workspace_required"


def test_the_header_names_the_workspace(user: Any, workspace: Any) -> None:
    second = provision_workspace(user, name="Second Brand")

    resolved = workspace_context.request_workspace(_Request(user, second.pk))

    assert resolved == second


def test_another_tenants_workspace_is_a_404_not_a_403(user: Any, other_user: Any) -> None:
    """Part 7 rule 3. A 403 would confirm the workspace exists."""
    theirs = provision_workspace(other_user, name="Not Yours")

    with pytest.raises(NotFoundError):
        workspace_context.request_workspace(_Request(user, theirs.pk))


def test_a_nonsense_header_is_a_404_too(user: Any, workspace: Any) -> None:
    with pytest.raises(NotFoundError):
        workspace_context.request_workspace(_Request(user, "not-a-number"))


def test_an_account_with_no_workspace_says_so(other_user: Any) -> None:
    with pytest.raises(OCCSError) as caught:
        workspace_context.request_workspace(_Request(other_user))

    assert caught.value.code == "no_workspace"


# -----------------------------------------------------------------------------
# P0-11 — permissions are authority, role is display
# -----------------------------------------------------------------------------
def test_stored_permissions_win_over_the_role(user: Any, workspace: Any) -> None:
    Membership.objects.filter(user=user, workspace=workspace).update(
        role=Role.VIEWER, permissions=["view", "approve"]
    )

    assert caller_permissions(_Request(user)) == {"view", "approve"}


def test_an_empty_permission_list_falls_back_to_the_preset(user: Any, workspace: Any) -> None:
    """Dual-read. Every row written before the expand migration has an empty
    list, and treating that as "no permissions" would lock out every existing
    user — a working deny is the hardest kind of bug to see."""
    Membership.objects.filter(user=user, workspace=workspace).update(
        role=Role.EDITOR, permissions=[]
    )

    assert "publish" in caller_permissions(_Request(user))
    assert "admin" not in caller_permissions(_Request(user))


def test_a_non_member_holds_nothing(other_user: Any, workspace: Any) -> None:
    with pytest.raises(OCCSError):
        caller_permissions(_Request(other_user))


# -----------------------------------------------------------------------------
# P0-12 — OWNER is not assignable
# -----------------------------------------------------------------------------
def test_owner_cannot_be_written_as_an_org_membership(user: Any, organization: Any) -> None:
    """Ownership is `Organization.owner`, a single FK. A membership row saying
    OWNER could drift out of sync with it, and then two places would disagree
    about who owns the company."""
    with pytest.raises(IntegrityError):
        OrganizationMembership.objects.create(organization=organization, user=user, role=Role.OWNER)


def test_provisioning_records_ownership_on_the_organization(user: Any, workspace: Any) -> None:
    organization = workspace.organization

    assert organization is not None
    assert organization.owner == user
    assert organization.memberships.get(user=user).role == Role.ADMIN


def test_registration_builds_the_whole_hierarchy(user: Any, workspace: Any) -> None:
    """P0-45: one transaction, or an account that every later request has to
    defend against."""
    assert Organization.objects.filter(pk=workspace.organization_id).exists()
    assert workspace.memberships.filter(user=user, role=Role.OWNER).exists()
    assert workspace.memberships.get(user=user).permissions


# -----------------------------------------------------------------------------
# P0-15 — the header reaches the API
# -----------------------------------------------------------------------------
def test_the_header_scopes_a_real_request(auth_client: Any, user: Any, workspace: Any) -> None:
    second = provision_workspace(user, name="Second Brand")

    response = auth_client.get(reverse("product-list"), HTTP_X_WORKSPACE_ID=str(second.pk))

    assert response.status_code == 200


def test_a_real_request_for_another_tenants_workspace_is_404(
    auth_client: Any, other_user: Any
) -> None:
    theirs = provision_workspace(other_user, name="Not Yours")

    response = auth_client.get(reverse("product-list"), HTTP_X_WORKSPACE_ID=str(theirs.pk))

    assert response.status_code == 404
