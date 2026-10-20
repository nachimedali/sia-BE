"""The emailed review page after a decision, in the sender's language (P7).

A client who approved yesterday and opens the email again should read what
they decided, not a bare "not found". So a spent approve link stays readable
until it expires, carries the decision, and refuses any second action with a
409 — the link can decide once. Request changes is the other decision, and it
needs a note: "changes" with nothing to change sends the team in circles.

The page is written in the language of the person who sent the link and shows
times in the workspace's zone, because the client reads it in the brand's
context, not their browser's.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from collaboration import review
from collaboration.models import ReviewLink, ReviewLinkPurpose
from content.models import PostStatus
from content.services.posts import create_post
from workspaces.models import ApprovalAction, ApprovalActionType
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


def _url(token: str, action: str = "") -> str:
    return f"/api/v1/review/{token}/{action + '/' if action else ''}"


@pytest.fixture
def post(advanced_workspace: Any, contributor_user: Any) -> Any:
    advanced_workspace.timezone = "Africa/Tunis"
    advanced_workspace.save(update_fields=["timezone"])
    return create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Launch copy"
    )


@pytest.fixture
def approve_link(post: Any, contributor_user: Any, admin_user: Any) -> tuple[Any, str]:
    admin_user.language = "fr"
    admin_user.save(update_fields=["language"])
    approvals.submit_for_review(post, actor=contributor_user)
    post.refresh_from_db()
    return review.issue(
        post, purpose=ReviewLinkPurpose.APPROVE, email="signoff@acme.test", created_by=admin_user
    )


@pytest.fixture
def guest_link(post: Any, admin_user: Any) -> tuple[Any, str]:
    return review.issue(
        post, purpose=ReviewLinkPurpose.GUEST_VIEW, email="client@acme.test", created_by=admin_user
    )


def test_the_packet_speaks_the_senders_language_in_the_workspaces_zone(
    client: Any, approve_link: Any
) -> None:
    _link, raw = approve_link

    body = client.get(_url(raw)).json()

    assert body["language"] == "fr"
    assert body["timezone"] == "Africa/Tunis"
    assert body["decision"] is None


def test_an_approved_link_still_opens_and_says_what_was_decided(
    client: Any, post: Any, approve_link: Any
) -> None:
    _link, raw = approve_link
    assert client.post(_url(raw, "approve")).status_code == 200

    body = client.get(_url(raw)).json()

    assert body["decision"]["decision"] == "approved"
    assert body["can_approve"] is False


def test_a_link_decides_once(client: Any, approve_link: Any) -> None:
    _link, raw = approve_link
    client.post(_url(raw, "approve"))

    again = client.post(_url(raw, "approve"))
    changes = client.post(_url(raw, "request-changes"), {"note": "Swap the photo"}, format="json")

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_decided"
    assert changes.status_code == 409


def test_request_changes_needs_a_note(client: Any, approve_link: Any) -> None:
    _link, raw = approve_link

    response = client.post(_url(raw, "request-changes"), {"note": "ok"}, format="json")

    assert response.status_code == 400
    assert "note" in str(response.json()["error"])


def test_request_changes_sends_the_post_back_and_records_the_guest(
    client: Any, post: Any, approve_link: Any
) -> None:
    link, raw = approve_link

    response = client.post(
        _url(raw, "request-changes"), {"note": "Swap the hero photo"}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["decision"]["decision"] == "changes_requested"
    post.refresh_from_db()
    assert post.status == PostStatus.CHANGES_REQUESTED
    action = ApprovalAction.objects.get(post=post, action=ApprovalActionType.REQUEST_CHANGES)
    assert action.guest_link_id == link.pk
    assert action.actor is None
    assert action.note == "Swap the hero photo"
    link.refresh_from_db()
    assert link.used_at is not None


def test_a_view_link_cannot_request_changes(client: Any, guest_link: Any) -> None:
    _link, raw = guest_link

    response = client.post(_url(raw, "request-changes"), {"note": "Swap the photo"}, format="json")

    assert response.status_code == 404


def test_a_decided_link_closes_when_it_expires_or_is_revoked(
    client: Any, approve_link: Any
) -> None:
    link, raw = approve_link
    client.post(_url(raw, "approve"))

    ReviewLink.objects.filter(pk=link.pk).update(
        expires_at=timezone.now() - dt.timedelta(minutes=1)
    )
    assert client.get(_url(raw)).status_code == 404

    ReviewLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() + dt.timedelta(days=1))
    review.revoke(link)
    assert client.get(_url(raw)).status_code == 404
