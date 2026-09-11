"""`POST /posts/{id}/submit|approve|request-changes|reject/` and the review
queue's own reads (design.md §7, §8.8; implementation.md Phase 13).

The comment endpoints that used to live here moved to `collaboration/` in
P2-01: internal discussion is no longer a limb of the approval feature, so
testing it as one would pin an arrangement that C-03 exists to end.
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
# C-02 — approval is required on every plan, so no plan gate stands in front
# of it. These three tests asserted the opposite until Phase 2: `submit`,
# `approve` and the trail were 402 below Advanced. A required step that the
# cheap plans cannot reach is a required step nobody can satisfy.
# -----------------------------------------------------------------------------
def test_submit_is_available_on_the_cheapest_plan(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    """`workspace` (the root fixture) is on Free."""
    post = create_post(workspace=workspace, author=user, master_body="Draft")

    response = auth_client.post(_url(post.pk, "submit"))

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.PENDING_REVIEW


def test_approve_is_available_on_the_cheapest_plan(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Draft")
    auth_client.post(_url(post.pk, "submit"))

    # The workspace's chain has no stages, so `allow_self_approve` never
    # applies and the author — who holds every permission as OWNER — is the
    # approver. That is the solo user's whole flow.
    response = auth_client.post(_url(post.pk, "approve"), {}, format="json")

    assert response.status_code == 200
    assert response.json()["status"] == PostStatus.APPROVED


def test_the_approval_trail_is_readable_on_the_cheapest_plan(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Draft")
    auth_client.post(_url(post.pk, "submit"))

    response = auth_client.get(_url(post.pk, "approvals"))

    assert response.status_code == 200
    assert [row["action"] for row in response.json()] == ["SUBMIT"]


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
# The review queue's own reads: the status filter, the author, the trail.
# All three exist because `/app/approvals` cannot be built correctly without
# them (implementation.md Phase 13, FE build item 6).
# -----------------------------------------------------------------------------
def test_status_filter_returns_the_whole_subset_not_a_filtered_page(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    """The point of filtering server-side: `max_page_size` is 100, so a client
    filtering a page of drafts would lose posts awaiting review behind them."""
    for index in range(30):
        post = create_post(
            workspace=advanced_workspace, author=contributor_user, master_body=f"Draft {index}"
        )
        # Every third post goes to review; the rest stay DRAFT and outnumber
        # them, so a page-1-then-filter client would come up short.
        if index % 3 == 0:
            approvals.submit_for_review(post, actor=contributor_user)

    response = client_as(contributor_user).get(
        "/api/v1/posts/", {"status": PostStatus.PENDING_REVIEW}
    )

    assert response.status_code == 200
    assert response.json()["count"] == 10
    assert {row["status"] for row in response.json()["results"]} == {PostStatus.PENDING_REVIEW}


def test_status_filter_accepts_several_statuses(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    pending = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Pending"
    )
    approvals.submit_for_review(pending, actor=contributor_user)
    approved = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Approved"
    )
    approvals.submit_for_review(approved, actor=contributor_user)
    approvals.approve(approved, actor=admin_user)
    create_post(workspace=advanced_workspace, author=contributor_user, master_body="Untouched")

    response = client_as(contributor_user).get(
        f"/api/v1/posts/?status={PostStatus.PENDING_REVIEW}&status={PostStatus.APPROVED}"
    )

    assert response.status_code == 200
    assert {row["id"] for row in response.json()["results"]} == {pending.pk, approved.pk}


def test_unknown_status_is_rejected_rather_than_silently_ignored(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    """A typo that returned every post would be a worse answer than an error:
    the caller asked for a subset and would be handed the whole table."""
    response = client_as(contributor_user).get("/api/v1/posts/", {"status": "PENDNIG_REVIEW"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_status"


def test_post_carries_its_author_email_for_the_review_queue(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(contributor_user).get(f"/api/v1/posts/{post.pk}/")

    assert response.json()["author_email"] == contributor_user.email


def test_author_email_is_read_only(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    response = client_as(contributor_user).patch(
        f"/api/v1/posts/{post.pk}/",
        {"author_email": "someone-else@example.com"},
        content_type="application/json",
    )

    assert response.status_code == 200
    post.refresh_from_db()
    assert post.author == contributor_user


def test_approval_history_is_this_posts_trail_oldest_first(
    client_as: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    approvals.submit_for_review(post, actor=contributor_user)
    approvals.request_changes(post, actor=admin_user, note="Soften the claim")
    approvals.submit_for_review(post, actor=contributor_user)
    other_post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Other"
    )
    approvals.submit_for_review(other_post, actor=contributor_user)

    response = client_as(contributor_user).get(f"/api/v1/posts/{post.pk}/approvals/")

    assert response.status_code == 200
    trail = response.json()
    assert [row["action"] for row in trail] == ["SUBMIT", "REQUEST_CHANGES", "SUBMIT"]
    assert trail[1]["actor_email"] == admin_user.email
    assert trail[1]["note"] == "Soften the claim"
    # The other post's own SUBMIT is not in this post's trail.
    assert {row["post"] for row in trail} == {post.pk}
