"""Visibility as the third tenancy dimension (P2-03, ship gate P2-G1).

**Proven at queryset level, not at the serializer.** Every assertion below
inspects what the queryset *loaded*, because a serializer that omits a field
has still read the row, still counted it in a page total, and still answered
200 where the honest answer was 404. Asserting on rendered JSON alone would
pass for an implementation with exactly that bug.

The HTTP half — a real guest token against `/g/{token}` — is in
`workspaces/tests/test_guest_review.py`, which cannot exist until P2-09 mints
the token. This file pins the layer underneath it.
"""

from __future__ import annotations

from typing import Any

import pytest

from collaboration.models import Thread
from collaboration.services import add_comment, open_thread
from common.visibility import (
    Audience,
    Visibility,
    VisibilityScopedQuerySetMixin,
    request_audience,
    visible_values,
)
from content.models import Post
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


class _Request:
    """The two attributes the mixins read. A stand-in rather than a real
    request because the point is the queryset, and building an HTTP request to
    assert on a `WHERE` clause would hide the thing being tested."""

    def __init__(self, workspace: Any, audience: Audience) -> None:
        self.workspace = workspace
        self.audience = audience


class _WorkspaceScoped:
    """Stands in for whatever sits under the visibility mixin in a real view —
    `WorkspaceScopedQuerySetMixin` plus a `queryset` attribute."""

    def __init__(self, model: Any, request: _Request) -> None:
        self.model = model
        self.request = request

    def get_queryset(self) -> Any:
        return self.model.objects.filter(workspace=self.request.workspace)


class Scoped(VisibilityScopedQuerySetMixin, _WorkspaceScoped):
    """The composition `ThreadViewSet` declares, isolated from its routing."""


@pytest.fixture
def internal_post(workspace: Any, user: Any) -> Any:
    return create_post(workspace=workspace, author=user, master_body="not for the client")


@pytest.fixture
def shared_post(workspace: Any, user: Any) -> Any:
    post = create_post(workspace=workspace, author=user, master_body="for review")
    post.visibility = Visibility.SHARED
    post.save(update_fields=["visibility"])
    return post


def test_a_post_is_internal_unless_someone_says_otherwise(internal_post: Any) -> None:
    """The default that cannot be walked back if it is wrong: by the time a
    draft has been read by the client, the mistake is already spent."""
    assert internal_post.visibility == Visibility.INTERNAL


# -----------------------------------------------------------------------------
# P2-G1 — an internal post is unreachable by a client, at queryset level
# -----------------------------------------------------------------------------
def test_a_client_queryset_never_loads_an_internal_post(
    workspace: Any, internal_post: Any, shared_post: Any
) -> None:
    client = Scoped(Post, _Request(workspace, Audience.CLIENT))

    loaded = client.get_queryset()

    assert list(loaded) == [shared_post]
    # By id, not merely by list: the gate is "unreachable", and a `.get()` that
    # succeeds would defeat a filter that only affects list pages.
    assert not loaded.filter(pk=internal_post.pk).exists()


def test_staff_load_both(workspace: Any, internal_post: Any, shared_post: Any) -> None:
    staff = Scoped(Post, _Request(workspace, Audience.STAFF))

    assert set(staff.get_queryset()) == {internal_post, shared_post}


def test_a_client_never_loads_an_internal_thread_on_a_shared_post(
    workspace: Any, shared_post: Any, user: Any
) -> None:
    """The subtle case: the post *is* shared, and the team is still allowed to
    talk about it privately underneath."""
    private = open_thread(shared_post, author=user, title="Legal check", body="Ask counsel.")
    visible = open_thread(
        shared_post,
        author=user,
        title="Which photo?",
        body="A or B?",
        visibility=Visibility.SHARED,
    )

    loaded = Scoped(Thread, _Request(workspace, Audience.CLIENT)).get_queryset()

    assert list(loaded) == [visible]
    assert not loaded.filter(pk=private.pk).exists()


def test_a_client_never_loads_an_internal_comment_in_a_shared_thread(
    shared_post: Any, user: Any
) -> None:
    """One shared thread can carry an internal aside — the two visibility
    fields are not redundant."""
    thread = open_thread(
        shared_post,
        author=user,
        title="Which photo?",
        body="A or B?",
        visibility=Visibility.SHARED,
    )
    aside = add_comment(thread, author=user, body="B is the one we overpaid for.")

    loaded = thread.comments.filter(visibility__in=visible_values(Audience.CLIENT))

    assert not loaded.filter(pk=aside.pk).exists()
    assert loaded.count() == 1


def test_the_audience_is_staff_unless_a_request_says_otherwise() -> None:
    """Guest access is the exception and has to announce itself. A missing
    attribute must not be able to switch staff access off — nor, read the other
    way, must a stray attribute be needed to keep it on."""

    class Bare:
        pass

    assert request_audience(Bare()) is Audience.STAFF
    assert request_audience(_Request(None, Audience.CLIENT)) is Audience.CLIENT


def test_visible_values_is_a_lookup_not_a_rank() -> None:
    """Written as a mapping so a third value later cannot be silently included
    by an inequality."""
    assert visible_values(Audience.CLIENT) == frozenset({Visibility.SHARED})
    assert visible_values(Audience.STAFF) == frozenset({Visibility.INTERNAL, Visibility.SHARED})
