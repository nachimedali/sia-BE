"""Annotations anchored to text (P2-02).

The single property under test throughout: **an annotation is never silently
moved.** Everything else here — the range check, the orphan flag, the recovery
— exists to serve that.
"""

from __future__ import annotations

from typing import Any

import pytest

from collaboration.models import Annotation
from collaboration.services import InvalidAnchorError, open_thread, reanchor
from content.services import revisions
from content.services.posts import create_post, update_post

pytestmark = pytest.mark.django_db

BODY = "Launch day is Monday. Doors open at nine."
THREADS_URL = "/api/v1/threads/"


@pytest.fixture
def post(workspace: Any, user: Any) -> Any:
    return create_post(workspace=workspace, author=user, master_body=BODY)


@pytest.fixture
def annotated(post: Any, user: Any) -> Any:
    """A comment pinned to "Monday" — index 14..20 of `BODY`."""
    thread = open_thread(
        post,
        author=user,
        title="Is that right?",
        body="I thought we agreed Tuesday.",
        anchor=(14, 20),
    )
    return thread.comments.get().annotation


def test_an_anchor_stores_the_text_it_was_written_about(annotated: Any) -> None:
    assert annotated.anchor_text == "Monday"
    assert annotated.orphaned is False


def test_editing_the_anchored_text_orphans_the_annotation(
    post: Any, annotated: Any, user: Any
) -> None:
    update_post(post, author=user, master_body="Launch day is Tuesday. Doors open at nine.")

    annotated.refresh_from_db()
    assert annotated.orphaned is True
    # Still carries what it was about, because the body no longer does.
    assert annotated.anchor_text == "Monday"


def test_an_orphaned_annotation_is_never_moved_to_the_new_text(
    post: Any, annotated: Any, user: Any
) -> None:
    """The failure this guards: a note reading "this claim is wrong" quietly
    relocated onto a different sentence. The range and the recorded text stay
    exactly as they were written."""
    update_post(post, author=user, master_body="Doors open at nine. Launch day is Monday.")

    annotated.refresh_from_db()
    # "Monday" *does* still occur in the body — at 39, not 14. Nothing searched
    # for it, so the annotation is orphaned rather than re-pointed.
    assert annotated.orphaned is True
    assert (annotated.anchor_start, annotated.anchor_end) == (14, 20)


def test_editing_elsewhere_leaves_a_matching_anchor_attached(
    post: Any, annotated: Any, user: Any
) -> None:
    update_post(post, author=user, master_body="Launch day is Monday. Doors open at eight.")

    annotated.refresh_from_db()
    assert annotated.orphaned is False


def test_putting_the_text_back_reattaches_it(post: Any, annotated: Any, user: Any) -> None:
    """Not a move: the range is the one originally chosen and the text under it
    is byte-identical to what was annotated."""
    update_post(post, author=user, master_body="Launch day is Tuesday. Doors open at nine.")
    assert Annotation.objects.get(pk=annotated.pk).orphaned is True

    update_post(post, author=user, master_body=BODY)

    assert Annotation.objects.get(pk=annotated.pk).orphaned is False


def test_restoring_a_revision_re_checks_anchors(post: Any, annotated: Any, user: Any) -> None:
    update_post(post, author=user, master_body="Launch day is Tuesday. Doors open at nine.")
    assert Annotation.objects.get(pk=annotated.pk).orphaned is True

    revisions.restore(post, sequence=1, author=user)

    assert Annotation.objects.get(pk=annotated.pk).orphaned is False


def test_a_range_outside_the_body_is_a_400(auth_client: Any, post: Any, user: Any) -> None:
    thread = open_thread(post, author=user, title="Hook", body="…")

    response = auth_client.post(
        f"{THREADS_URL}{thread.pk}/comments/",
        {"body": "on nothing", "anchor_start": 0, "anchor_end": 9_000},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_anchor"


def test_a_zero_width_anchor_is_refused(post: Any, user: Any) -> None:
    """A zero-width anchor selects nothing and can never mismatch — it would be
    an annotation that is permanently, meaninglessly valid."""
    with pytest.raises(InvalidAnchorError):
        open_thread(post, author=user, title="…", body="…", anchor=(5, 5))


def test_half_an_anchor_is_a_400(auth_client: Any, post: Any, user: Any) -> None:
    thread = open_thread(post, author=user, title="Hook", body="…")

    response = auth_client.post(
        f"{THREADS_URL}{thread.pk}/comments/",
        {"body": "half an anchor", "anchor_start": 3},
        format="json",
    )

    assert response.status_code == 400


def test_reanchor_reports_how_many_changed(post: Any, annotated: Any) -> None:
    post.master_body = "nothing like the original"
    post.save(update_fields=["master_body"])

    assert reanchor(post) == 1
    # Idempotent: a second pass finds nothing left to change.
    assert reanchor(post) == 0


def test_the_api_renders_an_annotation_beside_its_comment(
    auth_client: Any, post: Any, annotated: Any
) -> None:
    thread = annotated.comment.thread

    rows = auth_client.get(f"{THREADS_URL}{thread.pk}/comments/").json()

    assert rows[0]["annotation"] == {
        "anchor_start": 14,
        "anchor_end": 20,
        "anchor_text": "Monday",
        "orphaned": False,
    }
