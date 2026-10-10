"""Post version history (P1-08).

Append-only, diff-carrying, checkpointed, and pruned by plan. Four properties
worth naming because each has a failure mode that only shows up later:

* **Append-only.** A restore that rewrites history destroys the thing history
  is for. Restoring writes a *new* revision whose content happens to match an
  old one.
* **Diff plus periodic checkpoint.** Storing a full snapshot every time is
  simple and quadratic; storing only diffs makes reconstruction O(history) and
  makes retention unsafe, because deleting an old row silently invalidates
  every diff after it.
* **Retention that cannot orphan.** The trap in (2): a prune that respects only
  the date cutoff can delete the checkpoint the surviving diffs are anchored
  to, leaving rows that exist and cannot be reconstructed. The prune moves the
  cutoff back to a checkpoint.
* **An ambiguous zero.** `Plan.version_history_days` defaults to 0. A prune
  that reads 0 as "retain nothing" deletes every workspace's history the first
  night it runs against an unconfigured plan.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import time_machine
from django.contrib.auth import get_user_model
from django.utils import timezone

from billing.models import FeatureFlag
from billing.services.flags import CONTENT_MODEL_V2
from common.records import AppendOnlyError
from content.models import Platform, Post, PostRevision, PostStatus, PostTarget
from content.services import revisions
from content.services.media import ingest_media
from content.services.posts import create_post, set_alt_text, update_post
from content.tasks import prune_post_revisions
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

REVISIONS_URL = "/api/v1/posts/{pk}/revisions/"
RESTORE_URL = "/api/v1/posts/{pk}/revisions/{sequence}/restore/"


@pytest.fixture
def versioned_plan(workspace: Any, plans: dict[str, Any]) -> Any:
    """Pro keeps 90 days of history. Set explicitly rather than relying on the
    seed, so this file states the horizon it asserts against."""
    plan = plans["pro"]
    plan.version_history_days = 90
    plan.save(update_fields=["version_history_days"])
    workspace.organization.plan = plan
    workspace.organization.save(update_fields=["plan"])
    return plan


# -----------------------------------------------------------------------------
# Recording
# -----------------------------------------------------------------------------
def test_creating_a_post_records_its_first_revision(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="First draft")

    revision = post.revisions.get()
    assert revision.sequence == 1
    assert revision.is_checkpoint is True
    assert revision.snapshot["master_body"] == "First draft"
    assert revision.reason == "created"


def test_editing_the_body_records_a_revision_carrying_the_diff(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="First draft")
    update_post(post, master_body="Second draft")

    latest = post.revisions.order_by("-sequence").first()
    assert latest is not None
    assert latest.sequence == 2
    assert latest.diff["master_body"] == ["First draft", "Second draft"]


def test_a_save_that_changes_nothing_records_nothing(workspace: Any, user: Any) -> None:
    """History is what changed. A no-op save that appends a row makes the
    version list unreadable within a week of real use."""
    post = create_post(workspace=workspace, author=user, master_body="Same")
    update_post(post, master_body="Same")

    assert post.revisions.count() == 1


def test_changing_alt_text_is_a_revision(workspace: Any, user: Any, media_asset: Any) -> None:
    """Alt text is content — a restore that silently dropped it would be a
    restore that quietly removes an accessibility fix."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    set_alt_text(post, media_asset=media_asset, alt_text="A blue mug")

    latest = post.revisions.order_by("-sequence").first()
    assert latest is not None
    assert latest.sequence == 2
    assert latest.diff["media"][1][0]["alt_text"] == "A blue mug"


def test_a_target_override_is_a_revision(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Master")
    target = PostTarget.objects.create(post=post, platform=Platform.LINKEDIN)
    target.body_override = "Just for LinkedIn"
    target.save(update_fields=["body_override"])
    revisions.record(post, author=user, reason="edited")

    latest = post.revisions.order_by("-sequence").first()
    assert latest is not None
    assert latest.diff["targets"][1][0]["body_override"] == "Just for LinkedIn"


def test_every_nth_revision_is_a_full_checkpoint(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    for n in range(2, revisions.CHECKPOINT_EVERY + 2):
        update_post(post, master_body=f"v{n}")

    checkpoints = list(
        post.revisions.filter(is_checkpoint=True)
        .order_by("sequence")
        .values_list("sequence", flat=True)
    )
    assert checkpoints == [1, revisions.CHECKPOINT_EVERY + 1]


def test_a_non_checkpoint_revision_stores_no_snapshot(workspace: Any, user: Any) -> None:
    """The whole point of the diff: storing both would be storing the history
    twice."""
    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")

    second = post.revisions.get(sequence=2)
    assert second.is_checkpoint is False
    assert second.snapshot == {}


# -----------------------------------------------------------------------------
# Reconstruction
# -----------------------------------------------------------------------------
def test_state_at_reconstructs_any_revision_from_its_checkpoint(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    for n in range(2, 8):
        update_post(post, master_body=f"v{n}")

    for n in range(1, 8):
        assert revisions.state_at(post, n)["master_body"] == f"v{n}"


def test_state_at_an_unknown_sequence_raises(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    with pytest.raises(revisions.RevisionNotFoundError):
        revisions.state_at(post, 99)


# -----------------------------------------------------------------------------
# Append-only, and restore
# -----------------------------------------------------------------------------
def test_a_revision_cannot_be_edited(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    revision = post.revisions.get()
    revision.reason = "rewritten"

    with pytest.raises(AppendOnlyError):
        revision.save(update_fields=["reason"])


def test_a_revision_cannot_be_deleted(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    with pytest.raises(AppendOnlyError):
        post.revisions.get().delete()


def test_restoring_writes_a_new_revision_rather_than_rewinding(workspace: Any, user: Any) -> None:
    """The property that makes history trustworthy: nothing is ever removed,
    so "what did this look like on Tuesday" survives a restore."""
    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")

    restored = revisions.restore(post, sequence=1, author=user)

    assert restored.master_body == "v1"
    assert list(post.revisions.order_by("sequence").values_list("sequence", flat=True)) == [1, 2, 3]
    assert post.revisions.get(sequence=3).reason == "restored from revision 1"


def test_restoring_brings_back_media_order_and_alt_text(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    second = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset, second])
    set_alt_text(post, media_asset=media_asset, alt_text="The first one")

    update_post(post, media_asset_ids=[second])
    assert [a.pk for a in post.ordered_media()] == [second.pk]

    revisions.restore(post, sequence=2, author=user)

    attachments = post.ordered_attachments()
    assert [a.media_asset_id for a in attachments] == [media_asset.pk, second.pk]
    assert attachments[0].alt_text == "The first one"


def test_restoring_an_unknown_revision_is_404(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    response = auth_client.post(RESTORE_URL.format(pk=post.pk, sequence=99))
    assert response.status_code == 404


@pytest.mark.parametrize("status", [PostStatus.PUBLISHING, PostStatus.PUBLISHED])
def test_restoring_a_post_that_is_going_out_is_409(
    auth_client: Any, workspace: Any, user: Any, status: str
) -> None:
    """409, not 403 or 400: the request is well formed and the caller is
    entitled — the post is simply in a state that cannot accept an edit
    (Part 3's error table)."""
    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")
    post.status = status
    post.save(update_fields=["status"])

    response = auth_client.post(RESTORE_URL.format(pk=post.pk, sequence=1))
    assert response.status_code == 409


# -----------------------------------------------------------------------------
# The endpoints
# -----------------------------------------------------------------------------
def test_listing_revisions_newest_first(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    # `author` is who made *this* edit, not who owns the post — a revision with
    # no author is legitimate (autopilot, recurrence) and stays null rather
    # than borrowing the post's author.
    update_post(post, author=user, master_body="v2")

    body = auth_client.get(REVISIONS_URL.format(pk=post.pk)).json()
    assert [row["sequence"] for row in body] == [2, 1]
    assert body[0]["author_email"] == user.email
    assert body[0]["diff"]["master_body"] == ["v1", "v2"]


def test_restoring_over_the_api_returns_the_post(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")

    response = auth_client.post(RESTORE_URL.format(pk=post.pk, sequence=1))
    assert response.status_code == 200
    assert response.json()["master_body"] == "v1"


def test_another_workspaces_revisions_are_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = create_post(workspace=theirs, author=stranger, master_body="not yours")

    assert auth_client.get(REVISIONS_URL.format(pk=foreign.pk)).status_code == 404
    assert auth_client.post(RESTORE_URL.format(pk=foreign.pk, sequence=1)).status_code == 404


def test_another_organizations_revisions_are_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign = create_post(workspace=theirs, author=stranger, master_body="not yours")
    assert auth_client.get(REVISIONS_URL.format(pk=foreign.pk)).status_code == 404


# -----------------------------------------------------------------------------
# Retention — where data loss lives
# -----------------------------------------------------------------------------
def test_an_unconfigured_horizon_prunes_nothing(workspace: Any, user: Any, plans: Any) -> None:
    """`version_history_days` defaults to 0, and 0 is the column default, not
    a decision. Reading it as "retain nothing" would delete every history in
    the system the first night this job ran."""
    plan = workspace.organization.plan
    plan.version_history_days = 0
    plan.save(update_fields=["version_history_days"])

    post = create_post(workspace=workspace, author=user, master_body="v1")
    for n in range(2, 5):
        update_post(post, master_body=f"v{n}")
    PostRevision.objects.update(created_at=timezone.now() - dt.timedelta(days=3650))

    assert prune_post_revisions() == 0
    assert post.revisions.count() == 4


def test_an_unlimited_horizon_prunes_nothing(workspace: Any, user: Any) -> None:
    plan = workspace.organization.plan
    plan.version_history_days = -1
    plan.save(update_fields=["version_history_days"])

    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")
    PostRevision.objects.update(created_at=timezone.now() - dt.timedelta(days=3650))

    assert prune_post_revisions() == 0


def test_pruning_never_orphans_a_surviving_diff(
    workspace: Any, user: Any, versioned_plan: Any
) -> None:
    """The trap. Rows past the cutoff are deleted only back to a checkpoint,
    so everything retained can still be reconstructed."""
    with time_machine.travel("2026-01-01", tick=False):
        post = create_post(workspace=workspace, author=user, master_body="v1")
        for n in range(2, revisions.CHECKPOINT_EVERY + 5):
            update_post(post, master_body=f"v{n}")

    with time_machine.travel("2026-09-01", tick=False):
        prune_post_revisions()

    survivors = list(post.revisions.order_by("sequence"))
    assert survivors, "the prune deleted the whole history"
    assert survivors[0].is_checkpoint, "the oldest survivor must be reconstructable on its own"
    for revision in survivors:
        assert revisions.state_at(post, revision.sequence)["master_body"] == f"v{revision.sequence}"


def test_pruning_keeps_history_inside_the_horizon(
    workspace: Any, user: Any, versioned_plan: Any
) -> None:
    with time_machine.travel("2026-09-01", tick=False):
        post = create_post(workspace=workspace, author=user, master_body="v1")
        update_post(post, master_body="v2")
        assert prune_post_revisions() == 0
        assert post.revisions.count() == 2


def test_pruning_respects_each_organizations_own_plan(
    workspace: Any, user: Any, versioned_plan: Any, plans: Any
) -> None:
    """One tenant's short horizon must not reach another tenant's history."""
    stranger = get_user_model().objects.create_user(email="long@example.com", password="x")
    theirs = provision_workspace(stranger, name="Long Memory")
    long_plan = plans["advanced"]
    long_plan.version_history_days = 730
    long_plan.save(update_fields=["version_history_days"])
    theirs.organization.plan = long_plan
    theirs.organization.save(update_fields=["plan"])

    with time_machine.travel("2026-01-01", tick=False):
        mine = create_post(workspace=workspace, author=user, master_body="v1")
        for n in range(2, revisions.CHECKPOINT_EVERY + 5):
            update_post(mine, master_body=f"v{n}")
        yours = create_post(workspace=theirs, author=stranger, master_body="v1")
        for n in range(2, revisions.CHECKPOINT_EVERY + 5):
            update_post(yours, master_body=f"v{n}")

    with time_machine.travel("2026-09-01", tick=False):
        prune_post_revisions()

    assert mine.revisions.count() < revisions.CHECKPOINT_EVERY + 4
    assert yours.revisions.count() == revisions.CHECKPOINT_EVERY + 4


# -----------------------------------------------------------------------------
# The rollout flag
# -----------------------------------------------------------------------------
def test_with_the_flag_off_no_revision_is_recorded(workspace: Any, user: Any) -> None:
    """Part 3: flag off is pre-phase behaviour, not an error. Before Phase 1
    an edit recorded nothing, so with the flag off it records nothing — the
    edit itself still works."""
    FeatureFlag.objects.create(
        organization=workspace.organization, key=CONTENT_MODEL_V2, enabled=False
    )
    post = create_post(workspace=workspace, author=user, master_body="v1")
    update_post(post, master_body="v2")

    post.refresh_from_db()
    assert post.master_body == "v2"
    assert post.revisions.count() == 0


def test_with_the_flag_off_the_revision_routes_are_404(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="v1")
    FeatureFlag.objects.create(
        organization=workspace.organization, key=CONTENT_MODEL_V2, enabled=False
    )

    assert auth_client.get(REVISIONS_URL.format(pk=post.pk)).status_code == 404
    assert auth_client.post(RESTORE_URL.format(pk=post.pk, sequence=1)).status_code == 404


def test_one_organizations_flag_does_not_reach_another(workspace: Any, user: Any) -> None:
    stranger = get_user_model().objects.create_user(email="other@example.com", password="x")
    theirs = provision_workspace(stranger, name="Other Company")
    FeatureFlag.objects.create(
        organization=workspace.organization, key=CONTENT_MODEL_V2, enabled=False
    )

    create_post(workspace=workspace, author=user, master_body="mine")
    other_post = create_post(workspace=theirs, author=stranger, master_body="theirs")

    assert other_post.revisions.count() == 1


def test_a_change_is_recorded_even_when_the_caller_prefetched(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    """Regression: `PostViewSet` prefetches `media_attachments`, so by the time
    `set_alt_text` records, the cached rows still hold the *old* description.
    Comparing against the cache made the before and after look identical and
    recorded nothing — silently, which is the worst way for history to be
    wrong.
    """
    from django.db.models import Prefetch

    from content.models import PostMediaAttachment

    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    prefetched = Post.objects.prefetch_related(
        Prefetch("media_attachments", queryset=PostMediaAttachment.objects.all())
    ).get(pk=post.pk)
    list(prefetched.media_attachments.all())  # warm the cache, as a request does

    set_alt_text(prefetched, media_asset=media_asset, alt_text="Described", author=user)

    assert prefetched.revisions.count() == 2
