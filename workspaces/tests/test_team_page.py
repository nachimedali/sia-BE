"""What the team page needs (P5): pending invites, per-member permissions, and
removal that hands work over instead of orphaning it.

- Pending invites are listed with when they were sent and when they expire,
  and can be resent (a fresh 7 days, the old link dead) or revoked.
- A member's permissions are set one by one; the role stays a display preset.
- Before someone is removed, the page shows what they still own — threads
  assigned to them, posts they scheduled — and removal refuses to orphan that
  work: it is handed to another member, named in the request.

Admin-only, and another workspace's ids are 404, never 403.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from collaboration.models import Thread
from conftest import PASSWORD
from content.models import Post, PostStatus
from workspaces.models import Invitation, Membership, Role
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db


def _invites_url(workspace: Any) -> str:
    return f"/api/v1/workspaces/{workspace.pk}/invites/"


@pytest.fixture
def pending(advanced_workspace: Any, admin_user: Any) -> Invitation:
    invitation, _raw = Invitation.issue(
        workspace=advanced_workspace,
        email="new@example.com",
        role=Role.EDITOR,
        invited_by=admin_user,
    )
    return invitation


@pytest.fixture
def stranger() -> Any:
    """Someone with a workspace of their own and no business in this one."""
    person = get_user_model().objects.create_user(email="stranger@example.com", password=PASSWORD)
    provision_workspace(person, name="Elsewhere")
    return person


# -----------------------------------------------------------------------------
# Pending invites
# -----------------------------------------------------------------------------
def test_pending_invites_are_listed_with_their_clock(
    client_as: Any, advanced_workspace: Any, admin_user: Any, pending: Invitation
) -> None:
    accepted, _ = Invitation.issue(
        workspace=advanced_workspace, email="done@example.com", role=Role.VIEWER
    )
    Invitation.objects.filter(pk=accepted.pk).update(accepted_at=timezone.now())

    response = client_as(admin_user).get(_invites_url(advanced_workspace))

    assert response.status_code == 200
    rows = response.json()
    assert [row["email"] for row in rows] == ["new@example.com"]
    assert rows[0]["role"] == Role.EDITOR
    assert rows[0]["invited_by"] == admin_user.email
    assert rows[0]["account_exists"] is False
    assert rows[0]["expires_at"]
    assert "token" not in str(rows)


def test_resending_starts_the_clock_again_and_kills_the_old_link(
    client_as: Any, advanced_workspace: Any, admin_user: Any, pending: Invitation, outbox: list[Any]
) -> None:
    Invitation.objects.filter(pk=pending.pk).update(
        expires_at=timezone.now() + dt.timedelta(hours=2)
    )

    response = client_as(admin_user).post(f"{_invites_url(advanced_workspace)}{pending.pk}/resend/")

    assert response.status_code == 200
    fresh = response.json()
    assert fresh["id"] != pending.pk
    assert dt.datetime.fromisoformat(fresh["expires_at"]) > timezone.now() + dt.timedelta(days=6)
    pending.refresh_from_db()
    assert pending.revoked_at is not None
    assert outbox[-1].to == "new@example.com"


def test_revoking_kills_the_link(
    client_as: Any, advanced_workspace: Any, admin_user: Any, pending: Invitation
) -> None:
    response = client_as(admin_user).delete(f"{_invites_url(advanced_workspace)}{pending.pk}/")

    assert response.status_code == 204
    pending.refresh_from_db()
    assert pending.revoked_at is not None
    assert client_as(admin_user).get(_invites_url(advanced_workspace)).json() == []


def test_invites_are_admin_only(
    client_as: Any, advanced_workspace: Any, viewer_user: Any, pending: Invitation
) -> None:
    assert client_as(viewer_user).get(_invites_url(advanced_workspace)).status_code == 403
    assert (
        client_as(viewer_user)
        .delete(f"{_invites_url(advanced_workspace)}{pending.pk}/")
        .status_code
        == 403
    )


def test_another_workspaces_invites_are_404(
    client_as: Any, advanced_workspace: Any, stranger: Any, pending: Invitation
) -> None:
    other = client_as(stranger)

    assert other.get(_invites_url(advanced_workspace)).status_code == 404
    assert other.post(f"{_invites_url(advanced_workspace)}{pending.pk}/resend/").status_code == 404
    assert other.delete(f"{_invites_url(advanced_workspace)}{pending.pk}/").status_code == 404


def test_an_invite_id_from_another_workspace_is_404(
    client_as: Any, advanced_workspace: Any, admin_user: Any, stranger: Any
) -> None:
    theirs, _ = Invitation.issue(
        workspace=Membership.objects.get(user=stranger).workspace,
        email="x@example.com",
        role=Role.VIEWER,
    )

    assert (
        client_as(admin_user).delete(f"{_invites_url(advanced_workspace)}{theirs.pk}/").status_code
        == 404
    )


# -----------------------------------------------------------------------------
# Permissions, one by one
# -----------------------------------------------------------------------------
def test_permissions_are_set_one_by_one(
    client_as: Any, advanced_workspace: Any, admin_user: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    response = client_as(admin_user).patch(
        f"/api/v1/workspaces/members/{row.pk}/",
        {"permissions": ["view", "comment", "approve"]},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["permissions"] == ["approve", "comment", "view"]


def test_an_unknown_permission_is_refused(
    client_as: Any, advanced_workspace: Any, admin_user: Any, contributor_user: Any
) -> None:
    row = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)

    response = client_as(admin_user).patch(
        f"/api/v1/workspaces/members/{row.pk}/", {"permissions": ["fly"]}, format="json"
    )

    assert response.status_code == 400


def test_the_owners_permissions_cannot_be_narrowed(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    owner = Membership.objects.get(
        workspace=advanced_workspace, user=advanced_workspace.organization.owner
    )

    response = client_as(admin_user).patch(
        f"/api/v1/workspaces/members/{owner.pk}/", {"permissions": ["view"]}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_modify_owner"


# -----------------------------------------------------------------------------
# What someone owns, and handing it over
# -----------------------------------------------------------------------------
@pytest.fixture
def busy_member(advanced_workspace: Any, contributor_user: Any) -> Membership:
    draft = Post.objects.create(
        workspace=advanced_workspace, author=contributor_user, master_body="Draft under discussion"
    )
    for title in ("Price on slide 3?", "Caption check"):
        Thread.objects.create(
            workspace=advanced_workspace, post=draft, title=title, assignee=contributor_user
        )
    for body in ("Tuesday is roasting day", "Dinner for six", "Gift boxes"):
        Post.objects.create(
            workspace=advanced_workspace,
            author=contributor_user,
            master_body=body,
            status=PostStatus.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(days=2),
        )
    Post.objects.create(
        workspace=advanced_workspace,
        author=contributor_user,
        master_body="Old",
        status=PostStatus.PUBLISHED,
    )
    return Membership.objects.get(user=contributor_user, workspace=advanced_workspace)


def test_the_page_can_say_what_a_member_still_owns(
    client_as: Any, admin_user: Any, busy_member: Membership
) -> None:
    response = client_as(admin_user).get(f"/api/v1/workspaces/members/{busy_member.pk}/ownership/")

    assert response.status_code == 200
    body = response.json()
    assert body["threads"] == 2
    assert body["scheduled"] == 3
    assert {item["title"] for item in body["items"]} >= {"Price on slide 3?", "Dinner for six"}


def test_removal_refuses_to_orphan_work(
    client_as: Any, admin_user: Any, busy_member: Membership
) -> None:
    response = client_as(admin_user).delete(f"/api/v1/workspaces/members/{busy_member.pk}/")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "owns_items"
    assert Membership.objects.filter(pk=busy_member.pk).exists()


def test_removal_hands_the_work_to_the_member_named(
    client_as: Any,
    advanced_workspace: Any,
    admin_user: Any,
    busy_member: Membership,
    contributor_user: Any,
) -> None:
    heir = Membership.objects.get(user=admin_user, workspace=advanced_workspace)

    response = client_as(admin_user).delete(
        f"/api/v1/workspaces/members/{busy_member.pk}/", {"reassign_to": heir.pk}, format="json"
    )

    assert response.status_code == 204
    assert not Membership.objects.filter(pk=busy_member.pk).exists()
    assert Thread.objects.filter(assignee=admin_user).count() == 2
    assert Post.objects.filter(author=admin_user, status=PostStatus.SCHEDULED).count() == 3
    # Published history keeps its real author.
    assert Post.objects.filter(author=contributor_user, status=PostStatus.PUBLISHED).count() == 1
    assert advanced_workspace.audit_log.filter(verb="member.removed").exists()


def test_work_cannot_be_handed_to_someone_outside_the_workspace(
    client_as: Any, admin_user: Any, busy_member: Membership, stranger: Any
) -> None:
    theirs = Membership.objects.get(user=stranger)

    response = client_as(admin_user).delete(
        f"/api/v1/workspaces/members/{busy_member.pk}/", {"reassign_to": theirs.pk}, format="json"
    )

    assert response.status_code == 400
    assert Membership.objects.filter(pk=busy_member.pk).exists()


def test_another_workspaces_member_has_no_ownership_to_read(
    client_as: Any, busy_member: Membership, stranger: Any
) -> None:
    assert (
        client_as(stranger)
        .get(f"/api/v1/workspaces/members/{busy_member.pk}/ownership/")
        .status_code
        == 404
    )
