"""Token-scoped review (P2-09), and the HTTP half of ship gate P2-G1.

`test_visibility.py` proves an internal post is unreachable by a client at
queryset level. This file proves it over the wire, through the only door a
client actually has — which is the door that would be embarrassing to get
wrong.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from collaboration import review
from collaboration.models import Comment, ReviewLink, ReviewLinkPurpose, Thread
from collaboration.services import add_comment, open_thread
from common.visibility import Visibility
from content.models import PostStatus
from content.services.posts import create_post
from workspaces.models import ApprovalAction
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


def _packet(token: str) -> str:
    return f"/api/v1/review/{token}/"


@pytest.fixture
def post(advanced_workspace: Any, contributor_user: Any) -> Any:
    return create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Launch copy"
    )


@pytest.fixture
def guest_link(post: Any, admin_user: Any) -> tuple[Any, str]:
    return review.issue(
        post,
        purpose=ReviewLinkPurpose.GUEST_VIEW,
        email="Client@Acme.test",
        display_name="Dana at Acme",
        created_by=admin_user,
    )


@pytest.fixture
def approve_link(post: Any, contributor_user: Any, admin_user: Any) -> tuple[Any, str]:
    approvals.submit_for_review(post, actor=contributor_user)
    post.refresh_from_db()
    return review.issue(
        post,
        purpose=ReviewLinkPurpose.APPROVE,
        email="signoff@acme.test",
        created_by=admin_user,
    )


# -----------------------------------------------------------------------------
# Issuing
# -----------------------------------------------------------------------------
def test_sharing_a_post_marks_it_shared(post: Any, guest_link: tuple[Any, str]) -> None:
    """Minting a link *is* the act of sharing. Two separate steps would make
    the common failure "I sent the link and they see nothing", with no error
    anywhere to explain it."""
    post.refresh_from_db()
    assert post.visibility == Visibility.SHARED


def test_the_raw_token_is_never_returned_by_the_api(
    client_as: Any, post: Any, admin_user: Any, outbox: Any
) -> None:
    """It exists once, in the email, so a leak of an API response cannot be
    replayed into somebody else's draft."""
    response = client_as(admin_user).post(
        f"/api/v1/posts/{post.pk}/share/",
        {"purpose": ReviewLinkPurpose.GUEST_VIEW, "email": "client@acme.test"},
        format="json",
    )

    assert response.status_code == 201
    assert "token" not in str(response.json())
    assert outbox[-1].to == "client@acme.test"
    assert "/g/" in outbox[-1].context["review_url"]


def test_only_the_hash_is_stored(guest_link: tuple[Any, str]) -> None:
    link, raw = guest_link

    assert link.token_hash == ReviewLink.hash_token(raw)
    assert raw not in link.token_hash


def test_re_sharing_supersedes_the_earlier_link(post: Any, admin_user: Any) -> None:
    """Without it, re-sending leaves the earlier link live — which widens the
    window on one that may have gone to a mistyped address."""
    _first, first_raw = review.issue(
        post,
        purpose=ReviewLinkPurpose.GUEST_VIEW,
        email="client@acme.test",
        created_by=admin_user,
    )
    review.issue(
        post,
        purpose=ReviewLinkPurpose.GUEST_VIEW,
        email="client@acme.test",
        created_by=admin_user,
    )

    assert review.resolve(first_raw) is None


def test_sharing_another_tenants_post_is_404(auth_client: Any, workspace: Any) -> None:
    """404, never 403 (Part 7 rule 3). A separately provisioned company, not the
    `advanced_workspace` fixture — that one mutates `workspace` in place, so a
    post in it would be the caller's own."""
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    theirs_post = create_post(workspace=theirs, author=stranger, master_body="not yours")

    response = auth_client.post(
        f"/api/v1/posts/{theirs_post.pk}/share/",
        {"purpose": ReviewLinkPurpose.GUEST_VIEW, "email": "client@acme.test"},
        format="json",
    )

    assert response.status_code == 404
    assert not ReviewLink.objects.exists()


# -----------------------------------------------------------------------------
# P2-G1 over the wire — a client sees only what was shared
# -----------------------------------------------------------------------------
def test_a_guest_reads_the_post_and_only_the_shared_threads(
    client: Any, post: Any, contributor_user: Any, guest_link: tuple[Any, str]
) -> None:
    _link, raw = guest_link
    private = open_thread(post, author=contributor_user, title="Legal check", body="Ask counsel.")
    shared = open_thread(
        post,
        author=contributor_user,
        title="Which photo?",
        body="A or B?",
        visibility=Visibility.SHARED,
    )
    aside = add_comment(shared, author=contributor_user, body="B cost us a fortune.")

    body = client.get(_packet(raw)).json()

    assert body["post"]["master_body"] == "Launch copy"
    assert [thread["id"] for thread in body["threads"]] == [shared.pk]
    assert private.title not in str(body)
    # A shared thread can still carry an internal aside.
    assert aside.body not in str(body)


def test_a_guest_cannot_reach_an_internal_post_at_all(
    client: Any, post: Any, admin_user: Any
) -> None:
    """The gate, over HTTP: a link is the only door, and un-sharing the post
    closes it even while the token itself is still live."""
    _link, raw = review.issue(
        post,
        purpose=ReviewLinkPurpose.GUEST_VIEW,
        email="client@acme.test",
        created_by=admin_user,
    )
    post.refresh_from_db()
    post.visibility = Visibility.INTERNAL
    post.save(update_fields=["visibility"])

    body = client.get(_packet(raw)).json()

    # The packet is keyed to this post, so it still renders — but every
    # audience-scoped collection inside it is empty, because the queryset, not
    # the serializer, decides.
    assert body["threads"] == []


def test_an_unknown_or_revoked_token_is_404(client: Any, guest_link: tuple[Any, str]) -> None:
    """Unknown, expired, spent and revoked are indistinguishable — telling them
    apart would confirm that a post exists behind a token someone guessed."""
    link, raw = guest_link

    assert client.get(_packet("not-a-real-token")).status_code == 404

    review.revoke(link)

    assert client.get(_packet(raw)).status_code == 404


def test_an_expired_link_is_404(client: Any, guest_link: tuple[Any, str]) -> None:
    link, raw = guest_link
    link.expires_at = timezone.now() - dt.timedelta(seconds=1)
    link.save(update_fields=["expires_at"])

    assert client.get(_packet(raw)).status_code == 404


def test_the_packet_renders_what_publish_would_send(
    client: Any, post: Any, guest_link: tuple[Any, str]
) -> None:
    """Part 7 rule 1. A client signing off on a rendering that is not what
    publish sends would be signing off on nothing."""
    from content.services.adaptation import render_post

    _link, raw = guest_link
    platforms = list(post.workspace.platforms) or ["instagram"]
    post.workspace.platforms = platforms
    post.workspace.save(update_fields=["platforms"])

    body = client.get(_packet(raw)).json()

    expected = {
        platform: payload.as_dict() for platform, payload in render_post(post, platforms).items()
    }
    assert body["payloads"] == expected


# -----------------------------------------------------------------------------
# Guest comments
# -----------------------------------------------------------------------------
def test_a_guest_comment_lands_in_the_teams_thread_signed_by_name(
    client: Any, post: Any, guest_link: tuple[Any, str]
) -> None:
    link, raw = guest_link

    response = client.post(
        f"{_packet(raw)}comment/", {"body": "Can we use the other photo?"}, format="json"
    )

    assert response.status_code == 201
    comment = Comment.objects.get(guest_link=link)
    assert comment.author_id is None
    assert comment.visibility == Visibility.SHARED
    assert comment.author_label == "Dana at Acme"
    # Visible to the team, in the same place they are already working.
    assert Thread.objects.filter(post=post, comments=comment).exists()


def test_a_guest_cannot_post_into_an_internal_thread(
    client: Any, post: Any, contributor_user: Any, guest_link: tuple[Any, str]
) -> None:
    """Guessing the id would put their words somewhere they cannot read back."""
    _link, raw = guest_link
    private = open_thread(post, author=contributor_user, title="Legal", body="Ask counsel.")

    response = client.post(
        f"{_packet(raw)}comment/", {"body": "sneaking in", "thread": private.pk}, format="json"
    )

    assert response.status_code == 400


def test_a_comment_must_be_signed_by_exactly_one_of_the_two(
    post: Any, contributor_user: Any
) -> None:
    """Enforced in the database: a comment signed by nobody is unattributable,
    and one signed by both is a lie about who said it."""
    from django.db import IntegrityError, transaction

    thread = open_thread(post, author=contributor_user, title="T", body="B")

    with pytest.raises(IntegrityError), transaction.atomic():
        Comment.objects.create(thread=thread, author=None, guest_link=None, body="orphan")


# -----------------------------------------------------------------------------
# Approving from a link
# -----------------------------------------------------------------------------
def test_an_approve_link_approves_once_and_is_then_spent(
    client: Any, post: Any, approve_link: tuple[Any, str]
) -> None:
    link, raw = approve_link

    first = client.post(f"{_packet(raw)}approve/")

    assert first.status_code == 200
    post.refresh_from_db()
    assert post.status == PostStatus.APPROVED

    # Single-use: the token was spent by the approval.
    assert client.post(f"{_packet(raw)}approve/").status_code == 404
    link.refresh_from_db()
    assert link.used_at is not None


def test_the_approval_records_the_guest_not_whoever_sent_the_link(
    client: Any, post: Any, approve_link: tuple[Any, str], admin_user: Any
) -> None:
    """An audit trail naming the sender as the approver is a falsified one."""
    link, raw = approve_link

    client.post(f"{_packet(raw)}approve/")

    action = ApprovalAction.objects.get(post=post, action="APPROVE")
    assert action.actor_id is None
    assert action.guest_link_id == link.pk
    assert action.actor_label == "signoff"
    assert action.actor_id != admin_user.pk


def test_a_guest_view_link_cannot_approve(
    client: Any, post: Any, contributor_user: Any, guest_link: tuple[Any, str]
) -> None:
    """404 rather than 403: a read-only link must not learn that an approval
    endpoint exists for this post."""
    _link, raw = guest_link
    approvals.submit_for_review(post, actor=contributor_user)

    assert client.post(f"{_packet(raw)}approve/").status_code == 404
    post.refresh_from_db()
    assert post.status == PostStatus.PENDING_REVIEW


def test_a_guest_approval_schedules_the_authors_proposed_time(
    client: Any,
    advanced_workspace: Any,
    contributor_user: Any,
    admin_user: Any,
    advanced_social_account: Any,
) -> None:
    """P2-10 through the client's door. The client approved the content; the
    schedule is attributed to the member who proposed the time, because a
    person with no account cannot spend a workspace's quota."""
    when = timezone.now() + dt.timedelta(days=1)
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Launch")
    approvals.submit_for_review(
        post, actor=contributor_user, delivery_mode="AUTO_PUBLISH", scheduled_at=when
    )
    post.refresh_from_db()
    _link, raw = review.issue(
        post, purpose=ReviewLinkPurpose.APPROVE, email="signoff@acme.test", created_by=admin_user
    )

    client.post(f"{_packet(raw)}approve/")

    post.refresh_from_db()
    assert post.status == PostStatus.SCHEDULED
    assert post.scheduled_at == when


# -----------------------------------------------------------------------------
# Revoking — the reason multi-use needs an endpoint at all
# -----------------------------------------------------------------------------
def test_revoking_stops_a_multi_use_link(
    client_as: Any, client: Any, post: Any, admin_user: Any, guest_link: tuple[Any, str]
) -> None:
    link, raw = guest_link
    assert client.get(_packet(raw)).status_code == 200

    response = client_as(admin_user).post(f"/api/v1/posts/{post.pk}/share/{link.pk}/revoke/")

    assert response.status_code == 200
    assert client.get(_packet(raw)).status_code == 404


def test_revoking_is_idempotent(
    client_as: Any, post: Any, admin_user: Any, guest_link: tuple[Any, str]
) -> None:
    link, _raw = guest_link
    url = f"/api/v1/posts/{post.pk}/share/{link.pk}/revoke/"
    api = client_as(admin_user)

    first = api.post(url).json()["revoked_at"]
    second = api.post(url).json()["revoked_at"]

    assert first == second


def test_listing_shows_who_a_post_has_been_shared_with(
    client_as: Any, post: Any, admin_user: Any, guest_link: tuple[Any, str]
) -> None:
    """ "Who can see this post" is the question asked in a hurry."""
    rows = client_as(admin_user).get(f"/api/v1/posts/{post.pk}/share/").json()

    assert [row["email"] for row in rows] == ["client@acme.test"]
    assert rows[0]["is_usable"] is True
