"""Team membership (design.md §6.1; implementation.md Phase 13)."""

from __future__ import annotations

from typing import Any

import pytest

from common.exceptions import QuotaExceeded
from workspaces.models import Membership, Role
from workspaces.services import membership

pytestmark = pytest.mark.django_db


def _second_user(email: str = "new-teammate@example.com") -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email=email, password="x")


# -----------------------------------------------------------------------------
# add_member
# -----------------------------------------------------------------------------
def test_add_member_attaches_an_existing_account(advanced_workspace: Any, admin_user: Any) -> None:
    invitee = _second_user()

    added = membership.add_member(
        advanced_workspace, email=invitee.email, role=Role.EDITOR, invited_by=admin_user
    )

    assert added.user_id == invitee.pk
    assert added.role == Role.EDITOR
    assert added.invited_by_id == admin_user.pk


def test_add_member_is_case_insensitive_on_email(advanced_workspace: Any, admin_user: Any) -> None:
    invitee = _second_user(email="Mixed.Case@Example.com")

    added = membership.add_member(
        advanced_workspace, email="mixed.case@example.com", role=Role.VIEWER, invited_by=admin_user
    )

    assert added.user_id == invitee.pk


def test_add_member_refuses_an_unknown_email(advanced_workspace: Any, admin_user: Any) -> None:
    with pytest.raises(membership.NoAccountForEmailError):
        membership.add_member(
            advanced_workspace,
            email="nobody@example.com",
            role=Role.VIEWER,
            invited_by=admin_user,
        )


def test_add_member_refuses_owner_as_an_assignable_role(
    advanced_workspace: Any, admin_user: Any
) -> None:
    invitee = _second_user()

    with pytest.raises(membership.RoleNotAssignableError):
        membership.add_member(
            advanced_workspace, email=invitee.email, role=Role.OWNER, invited_by=admin_user
        )


def test_add_member_refuses_a_duplicate(advanced_workspace: Any, admin_user: Any) -> None:
    invitee = _second_user()
    membership.add_member(
        advanced_workspace, email=invitee.email, role=Role.VIEWER, invited_by=admin_user
    )

    with pytest.raises(membership.MemberAlreadyExistsError) as excinfo:
        membership.add_member(
            advanced_workspace, email=invitee.email, role=Role.EDITOR, invited_by=admin_user
        )
    assert excinfo.value.status_code == 409


def test_add_member_enforces_the_workspace_member_cap(
    advanced_workspace: Any, admin_user: Any, plans: Any
) -> None:
    """I8: the cap lives on `Plan`, never a literal here."""
    plan = plans["advanced"]
    plan.max_workspace_members = 2  # owner + admin_user already fills it
    plan.save(update_fields=["max_workspace_members"])
    invitee = _second_user()

    with pytest.raises(QuotaExceeded) as excinfo:
        membership.add_member(
            advanced_workspace, email=invitee.email, role=Role.VIEWER, invited_by=admin_user
        )
    assert excinfo.value.status_code == 402


# -----------------------------------------------------------------------------
# change_role / remove_member
# -----------------------------------------------------------------------------
def test_change_role_updates_an_existing_member(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    updated = membership.change_role(row, role=Role.EDITOR)

    assert updated.role == Role.EDITOR


def test_change_role_refuses_to_touch_the_owners_row(advanced_workspace: Any, user: Any) -> None:
    owner_row = Membership.objects.get(user=user, workspace=advanced_workspace)

    with pytest.raises(membership.CannotModifyOwnerError):
        membership.change_role(owner_row, role=Role.ADMIN)


def test_change_role_refuses_owner_as_the_new_role(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    with pytest.raises(membership.RoleNotAssignableError):
        membership.change_role(row, role=Role.OWNER)


def test_remove_member_deletes_the_row(advanced_workspace: Any, contributor_user: Any) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    membership.remove_member(row)

    assert not Membership.objects.filter(pk=row.pk).exists()


def test_remove_member_refuses_to_remove_the_owner(advanced_workspace: Any, user: Any) -> None:
    owner_row = Membership.objects.get(user=user, workspace=advanced_workspace)

    with pytest.raises(membership.CannotModifyOwnerError):
        membership.remove_member(owner_row)

    assert Membership.objects.filter(pk=owner_row.pk).exists()
