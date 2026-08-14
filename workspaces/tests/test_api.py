"""`/workspaces/settings/`, `/workspaces/members/`, `/workspaces/audit-log/`
(design.md §7, §8.8; implementation.md Phase 13).
"""

from __future__ import annotations

from typing import Any

import pytest

from workspaces.models import Membership, Role
from workspaces.services import approvals

pytestmark = pytest.mark.django_db

SETTINGS_URL = "/api/v1/workspaces/settings/"
MEMBERS_URL = "/api/v1/workspaces/members/"
AUDIT_LOG_URL = "/api/v1/workspaces/audit-log/"


# -----------------------------------------------------------------------------
# WorkspaceSettingsView
# -----------------------------------------------------------------------------
def test_reading_settings_is_open_to_any_plan(auth_client: Any, workspace: Any) -> None:
    """Free/Pro can always see `requires_approval` is `False` — the toggle
    cannot have been switched on without the feature (`patch` is gated)."""
    response = auth_client.get(SETTINGS_URL)

    assert response.status_code == 200
    assert response.json()["requires_approval"] is False


def test_toggling_settings_requires_the_feature(auth_client: Any, workspace: Any) -> None:
    response = auth_client.patch(SETTINGS_URL, {"requires_approval": True}, format="json")

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_available"


def test_toggling_settings_requires_admin(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    response = client_as(contributor_user).patch(
        SETTINGS_URL, {"requires_approval": False}, format="json"
    )

    assert response.status_code == 403


def test_an_admin_can_toggle_requires_approval(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    response = client_as(admin_user).patch(
        SETTINGS_URL, {"requires_approval": False}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["requires_approval"] is False
    advanced_workspace.refresh_from_db()
    assert advanced_workspace.requires_approval is False
    assert advanced_workspace.audit_log.filter(verb="workspace.requires_approval").count() == 1


# -----------------------------------------------------------------------------
# MembershipViewSet
# -----------------------------------------------------------------------------
def test_any_member_can_list_the_roster(
    client_as: Any, advanced_workspace: Any, viewer_user: Any, admin_user: Any
) -> None:
    response = client_as(viewer_user).get(MEMBERS_URL)

    assert response.status_code == 200
    emails = {row["user_email"] for row in response.json()}
    assert emails == {advanced_workspace.owner.email, viewer_user.email, admin_user.email}


def test_the_owners_row_is_flagged(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    response = client_as(admin_user).get(MEMBERS_URL)

    rows = {row["user_email"]: row for row in response.json()}
    assert rows[advanced_workspace.owner.email]["is_owner"] is True
    assert rows[admin_user.email]["is_owner"] is False


def test_a_viewer_cannot_add_a_member(
    client_as: Any, advanced_workspace: Any, viewer_user: Any
) -> None:
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_user(email="new@example.com", password="x")

    response = client_as(viewer_user).post(
        MEMBERS_URL, {"email": "new@example.com", "role": Role.EDITOR}, format="json"
    )

    assert response.status_code == 403


def test_an_admin_can_add_an_existing_account(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    from django.contrib.auth import get_user_model

    new_user = get_user_model().objects.create_user(email="new@example.com", password="x")

    response = client_as(admin_user).post(
        MEMBERS_URL, {"email": "new@example.com", "role": Role.EDITOR}, format="json"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user_email"] == "new@example.com"
    assert body["role"] == Role.EDITOR
    assert Membership.objects.filter(user=new_user, workspace=advanced_workspace).exists()


def test_adding_an_unknown_email_is_a_clear_400(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    response = client_as(admin_user).post(
        MEMBERS_URL, {"email": "nobody@example.com", "role": Role.VIEWER}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_account_for_email"


def test_an_admin_can_change_a_members_role(
    client_as: Any, advanced_workspace: Any, admin_user: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    response = client_as(admin_user).patch(
        f"{MEMBERS_URL}{row.pk}/", {"role": Role.EDITOR}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["role"] == Role.EDITOR


def test_the_owners_row_cannot_be_changed(
    client_as: Any, advanced_workspace: Any, admin_user: Any, user: Any
) -> None:
    owner_row = Membership.objects.get(user=user, workspace=advanced_workspace)

    response = client_as(admin_user).patch(
        f"{MEMBERS_URL}{owner_row.pk}/", {"role": Role.ADMIN}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_modify_owner"


def test_an_admin_can_remove_a_member(
    client_as: Any, advanced_workspace: Any, admin_user: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    response = client_as(admin_user).delete(f"{MEMBERS_URL}{row.pk}/")

    assert response.status_code == 204
    assert not Membership.objects.filter(pk=row.pk).exists()


def test_cross_workspace_membership_access_404s(
    client_as: Any, advanced_workspace: Any, admin_user: Any, plans: Any
) -> None:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    other_owner = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_workspace = provision_workspace(other_owner, name="Someone Else")
    other_row = Membership.objects.get(user=other_owner, workspace=other_workspace)

    response = client_as(admin_user).get(f"{MEMBERS_URL}{other_row.pk}/")

    assert response.status_code == 404


# -----------------------------------------------------------------------------
# AuditLogView
# -----------------------------------------------------------------------------
def test_audit_log_is_gated_to_advanced(auth_client: Any, workspace: Any) -> None:
    response = auth_client.get(AUDIT_LOG_URL)

    assert response.status_code == 402


def test_audit_log_lists_workspace_history(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    from content.services.posts import create_post

    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.submit_for_review(post, actor=contributor_user)
    approvals.approve(post, actor=admin_user)

    response = client_as(admin_user).get(AUDIT_LOG_URL)

    assert response.status_code == 200
    verbs = [row["verb"] for row in response.json()["results"]]
    assert "post.submit" in verbs
    assert "post.approve" in verbs


def test_audit_log_does_not_leak_across_workspaces(
    client_as: Any, advanced_workspace: Any, admin_user: Any, plans: Any
) -> None:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    other_owner = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_workspace = provision_workspace(other_owner, name="Someone Else")
    approvals.log(workspace=other_workspace, actor=other_owner, verb="not.yours")

    response = client_as(admin_user).get(AUDIT_LOG_URL)

    verbs = [row["verb"] for row in response.json()["results"]]
    assert "not.yours" not in verbs
