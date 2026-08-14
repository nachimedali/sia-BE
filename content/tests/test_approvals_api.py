"""`POST /posts/{id}/submit|approve|request-changes|reject/`, comments, and
the resolve action (design.md §7, §8.8; implementation.md Phase 13).
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import PostStatus
from content.services.posts import create_post
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


def _url(post_id: int, action: str) -> str:
    return f"/api/v1/posts/{post_id}/{action}/"


# -----------------------------------------------------------------------------
# test_approval_workflow_gated_to_advanced
# -----------------------------------------------------------------------------
def test_approval_workflow_gated_to_advanced(auth_client: Any, workspace: Any, user: Any) -> None:
    """`workspace` (the root fixture) is on Free — no `approval_workflow`."""
    post = create_post(workspace=workspace, author=user, master_body="Draft")

    response = auth_client.post(_url(post.pk, "submit"))

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]["suggested_plan"] == "advanced"


def test_approve_is_also_gated_to_advanced(
    auth_client: Any, workspace: Any, user: Any, plans: Any
) -> None:
    """Pro has `auto_publish` but not `approval_workflow` — the two paid tiers
    must not be conflated."""
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    post = create_post(workspace=workspace, author=user, master_body="Draft")

    assert auth_client.post(_url(post.pk, "approve")).status_code == 402


# -----------------------------------------------------------------------------
# Role gates
# -----------------------------------------------------------------------------
def test_a_contributor_may_submit(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(contributor_user).post(_url(post.pk, "submit"))

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.PENDING_REVIEW


def test_a_viewer_may_not_submit(
    client_as: Any, advanced_workspace: Any, viewer_user: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(viewer_user).post(_url(post.pk, "submit"))

    assert response.status_code == 403


def test_a_contributor_may_not_approve(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.submit_for_review(post, actor=contributor_user)

    response = client_as(contributor_user).post(_url(post.pk, "approve"))

    assert response.status_code == 403


def test_an_admin_may_approve_request_changes_and_reject(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    admin = client_as(admin_user)

    for action, expected_status in (
        ("approve", PostStatus.APPROVED),
        ("reject", PostStatus.REJECTED),
    ):
        post = create_post(
            workspace=advanced_workspace, author=contributor_user, master_body="Draft"
        )
        approvals.submit_for_review(post, actor=contributor_user)

        response = admin.post(_url(post.pk, action))

        assert response.status_code == 200
        assert response.json()["status"] == expected_status


def test_request_changes_requires_a_note(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.submit_for_review(post, actor=contributor_user)

    response = client_as(admin_user).post(_url(post.pk, "request-changes"), {"note": ""})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "note_required"


def test_request_changes_with_a_note_succeeds(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.submit_for_review(post, actor=contributor_user)

    response = client_as(admin_user).post(
        _url(post.pk, "request-changes"), {"note": "Shorten the hook"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.CHANGES_REQUESTED


# -----------------------------------------------------------------------------
# test_illegal_state_transition_returns_409
# -----------------------------------------------------------------------------
def test_illegal_state_transition_returns_409(
    client_as: Any, advanced_workspace: Any, admin_user: Any, contributor_user: Any
) -> None:
    """A `DRAFT` post has never been submitted — approving it is illegal."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(admin_user).post(_url(post.pk, "approve"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT


# -----------------------------------------------------------------------------
# Comments
# -----------------------------------------------------------------------------
def test_reading_comments_is_open_to_a_viewer(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, viewer_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.add_comment(post, author=contributor_user, body="A note")

    response = client_as(viewer_user).get(_url(post.pk, "comments"))

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_a_contributor_can_post_a_comment(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(contributor_user).post(
        _url(post.pk, "comments"), {"body": "Consider a shorter hook"}
    )

    assert response.status_code == 201
    assert response.json()["author_email"] == contributor_user.email


def test_a_viewer_cannot_post_a_comment(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, viewer_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(viewer_user).post(_url(post.pk, "comments"), {"body": "Hi"})

    assert response.status_code == 403


def test_a_reply_must_belong_to_the_same_post(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    other_post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Other"
    )
    foreign_comment = approvals.add_comment(other_post, author=contributor_user, body="Elsewhere")

    response = client_as(contributor_user).post(
        _url(post.pk, "comments"), {"body": "Reply", "parent": foreign_comment.pk}
    )

    assert response.status_code == 400


def test_resolving_a_comment(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    comment = approvals.add_comment(post, author=contributor_user, body="A note")

    response = client_as(contributor_user).post(
        f"/api/v1/posts/{post.pk}/comments/{comment.pk}/resolve/"
    )

    assert response.status_code == 200
    assert response.json()["resolved_at"] is not None


def test_resolving_a_comment_from_a_different_post_404s(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    other_post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Other"
    )
    foreign_comment = approvals.add_comment(other_post, author=contributor_user, body="Elsewhere")

    response = client_as(contributor_user).post(
        f"/api/v1/posts/{post.pk}/comments/{foreign_comment.pk}/resolve/"
    )

    assert response.status_code == 404
