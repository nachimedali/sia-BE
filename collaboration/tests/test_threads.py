"""The internal discussion surface (P2-01, C-03).

The property that matters most here is the one in the module docstring of
`collaboration/models.py`: a thread is **independent of any approval**. Half of
these tests exercise threads on a plain `DRAFT` with nothing in flight, because
that is the case the old `PostComment` could not serve.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from collaboration.models import Comment, Reaction, Thread, ThreadStatus
from collaboration.services import InvalidReactionError, open_thread, react
from content.services.posts import create_post
from workspaces.models import Membership, Role
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

THREADS_URL = "/api/v1/threads/"


@pytest.fixture
def post(workspace: Any, user: Any) -> Any:
    return create_post(workspace=workspace, author=user, master_body="Launch day is Monday.")


def test_opening_a_thread_needs_no_approval_in_flight(
    auth_client: Any, post: Any, user: Any
) -> None:
    """C-03's whole point. The post is a `DRAFT`; nothing has been submitted."""
    response = auth_client.post(
        THREADS_URL,
        {"post": post.pk, "title": "Tighten the hook", "body": "The first line is flat."},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Tighten the hook"
    assert body["status"] == ThreadStatus.OPEN
    assert body["comment_count"] == 1
    assert body["opened_by_email"] == user.email

    thread = Thread.objects.get(pk=body["id"])
    assert thread.workspace_id == post.workspace_id
    assert thread.comments.get().body == "The first line is flat."


def test_a_thread_and_its_first_comment_are_one_transaction(post: Any, user: Any) -> None:
    """A thread with no comment is a title nobody can answer."""
    thread = open_thread(post, author=user, title="Check the claim", body="Is Monday right?")

    assert thread.comments.count() == 1


def test_replies_thread_under_their_parent(auth_client: Any, post: Any, user: Any) -> None:
    thread = open_thread(post, author=user, title="Hook", body="Flat.")
    first = thread.comments.get()

    response = auth_client.post(
        f"{THREADS_URL}{thread.pk}/comments/",
        {"body": "Agreed — try a question.", "parent": first.pk},
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["parent"] == first.pk


def test_a_reply_cannot_be_threaded_onto_another_conversation(
    auth_client: Any, post: Any, user: Any
) -> None:
    """A tenancy leak wearing a reply's clothes: the thread in the URL is
    legitimately the caller's, so only the serializer can catch this."""
    mine = open_thread(post, author=user, title="Mine", body="…")
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs_workspace = provision_workspace(stranger, name="Another Company")
    theirs_post = create_post(workspace=theirs_workspace, author=stranger, master_body="theirs")
    theirs = open_thread(theirs_post, author=stranger, title="Theirs", body="…")

    response = auth_client.post(
        f"{THREADS_URL}{mine.pk}/comments/",
        {"body": "sneaking in", "parent": theirs.comments.get().pk},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_done_records_who_resolved_it_and_reopening_clears_that(
    auth_client: Any, post: Any, user: Any
) -> None:
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    done = auth_client.post(
        f"{THREADS_URL}{thread.pk}/status/", {"status": ThreadStatus.DONE}, format="json"
    ).json()
    assert done["resolved_by"] == user.pk
    assert done["resolved_at"] is not None

    # A reopened thread that still reads "resolved by Jordan on Tuesday"
    # invites everyone to assume it is handled.
    reopened = auth_client.post(
        f"{THREADS_URL}{thread.pk}/status/", {"status": ThreadStatus.OPEN}, format="json"
    ).json()
    assert reopened["resolved_by"] is None
    assert reopened["resolved_at"] is None


def test_later_is_not_done(auth_client: Any, post: Any, user: Any) -> None:
    """ "Not now" and "handled" are different answers, and a surface that
    conflates them is measuring nothing."""
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    body = auth_client.post(
        f"{THREADS_URL}{thread.pk}/status/", {"status": ThreadStatus.LATER}, format="json"
    ).json()

    assert body["status"] == ThreadStatus.LATER
    assert body["resolved_at"] is None


def test_an_assignee_must_be_a_member_of_this_workspace(
    auth_client: Any, post: Any, user: Any, other_user: Any
) -> None:
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    response = auth_client.post(
        f"{THREADS_URL}{thread.pk}/assign/", {"assignee": other_user.pk}, format="json"
    )

    assert response.status_code == 400


def test_assigning_and_clearing(auth_client: Any, post: Any, user: Any, workspace: Any) -> None:
    teammate = get_user_model().objects.create_user(email="teammate@example.com", password="x")
    Membership.objects.create(user=teammate, workspace=workspace, role=Role.EDITOR)
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    assigned = auth_client.post(
        f"{THREADS_URL}{thread.pk}/assign/", {"assignee": teammate.pk}, format="json"
    ).json()
    assert assigned["assignee_email"] == teammate.email

    cleared = auth_client.post(
        f"{THREADS_URL}{thread.pk}/assign/", {"assignee": None}, format="json"
    ).json()
    assert cleared["assignee"] is None


def test_reacting_twice_is_one_reaction(auth_client: Any, post: Any, user: Any) -> None:
    thread = open_thread(post, author=user, title="Hook", body="Flat.")
    comment = thread.comments.get()
    url = f"{THREADS_URL}{thread.pk}/comments/{comment.pk}/reactions/"

    auth_client.post(url, {"emoji": "👍"}, format="json")
    response = auth_client.post(url, {"emoji": "👍"}, format="json")

    assert response.status_code == 200
    assert Reaction.objects.filter(comment=comment).count() == 1
    assert [row["emoji"] for row in response.json()["reactions"]] == ["👍"]


def test_removing_a_reaction_is_idempotent(auth_client: Any, post: Any, user: Any) -> None:
    thread = open_thread(post, author=user, title="Hook", body="Flat.")
    comment = thread.comments.get()
    url = f"{THREADS_URL}{thread.pk}/comments/{comment.pk}/reactions/?emoji=%F0%9F%91%8D"

    auth_client.delete(url)
    response = auth_client.delete(url)

    assert response.status_code == 200
    assert Reaction.objects.filter(comment=comment).count() == 0


def test_a_multi_codepoint_emoji_is_one_reaction(post: Any, user: Any) -> None:
    """A flag is two code points and a family sequence can reach eleven —
    counting code points would refuse both."""
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    reaction = react(thread.comments.get(), user=user, emoji="👨‍👩‍👧‍👦")

    assert reaction.emoji == "👨‍👩‍👧‍👦"


def test_a_paragraph_is_not_a_reaction(post: Any, user: Any) -> None:
    """A reaction bar with no length cap is a second comment field with no
    moderation behind it."""
    thread = open_thread(post, author=user, title="Hook", body="Flat.")

    with pytest.raises(InvalidReactionError):
        react(thread.comments.get(), user=user, emoji="this is not an emoji at all")


def test_threads_filter_by_post_and_status(auth_client: Any, post: Any, user: Any) -> None:
    other = create_post(workspace=post.workspace, author=user, master_body="another")
    open_thread(post, author=user, title="On this post", body="…")
    open_thread(other, author=user, title="On the other", body="…")

    rows = auth_client.get(f"{THREADS_URL}?post={post.pk}").json()["results"]

    assert [row["title"] for row in rows] == ["On this post"]


def test_reading_needs_view_and_writing_needs_comment(
    client_as: Any, post: Any, workspace: Any
) -> None:
    """A VIEWER preset holds `view` and `analyze` — it may read the discussion
    and may not join it."""
    watcher = get_user_model().objects.create_user(email="watcher@example.com", password="x")
    Membership.objects.create(user=watcher, workspace=workspace, role=Role.VIEWER)
    api = client_as(watcher)

    assert api.get(THREADS_URL).status_code == 200

    denied = api.post(THREADS_URL, {"post": post.pk, "title": "no", "body": "no"}, format="json")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


# -----------------------------------------------------------------------------
# Tenancy — 404, never 403, across workspace and organization (Part 7 rule 3)
# -----------------------------------------------------------------------------
def test_another_organizations_thread_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs_workspace = provision_workspace(stranger, name="Another Company")
    theirs_post = create_post(workspace=theirs_workspace, author=stranger, master_body="theirs")
    theirs = open_thread(theirs_post, author=stranger, title="Theirs", body="…")

    assert auth_client.get(f"{THREADS_URL}{theirs.pk}/").status_code == 404
    assert auth_client.get(f"{THREADS_URL}{theirs.pk}/comments/").status_code == 404


def test_a_second_workspace_in_my_own_organization_is_404(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    """Cross-*workspace* within one company — the dimension a single-workspace
    account could never exercise."""
    from workspaces.services.provisioning import provision_extra_workspace

    sibling = provision_extra_workspace(user=user, name="Second Brand")
    sibling_post = create_post(workspace=sibling, author=user, master_body="sibling")
    theirs = open_thread(sibling_post, author=user, title="Sibling", body="…")

    # The header names the *first* workspace, so the sibling's thread is out of
    # scope even though the caller is a legitimate member of both.
    response = auth_client.get(f"{THREADS_URL}{theirs.pk}/", HTTP_X_WORKSPACE_ID=str(workspace.pk))

    assert response.status_code == 404


def test_a_thread_cannot_be_opened_on_another_tenants_post(
    auth_client: Any, workspace: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs_workspace = provision_workspace(stranger, name="Another Company")
    theirs_post = create_post(workspace=theirs_workspace, author=stranger, master_body="theirs")

    response = auth_client.post(
        THREADS_URL, {"post": theirs_post.pk, "title": "…", "body": "…"}, format="json"
    )

    assert response.status_code == 400
    assert Comment.objects.count() == 0
