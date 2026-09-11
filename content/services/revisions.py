"""Post version history (P1-08).

Three operations, and each one exists because the obvious shortcut has a
failure mode:

* `record` appends a revision **only when something changed**. A row per save
  makes the version list unreadable inside a week of real use.
* `state_at` reconstructs any revision by walking forward from its checkpoint,
  so reconstruction cost is bounded by `CHECKPOINT_EVERY` rather than by the
  length of the history.
* `restore` writes a **new** revision whose content matches an old one. It
  never removes or rewinds rows — history a restore can rewrite is not
  history.

Nothing here runs on a signal (Part 7 rule 8). `create_post`, `update_post`
and `set_alt_text` call `record` explicitly, which is also what makes it
possible to record one revision for an edit that touches three tables.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.db import transaction

from billing.services.flags import CONTENT_MODEL_V2, flag_enabled
from common.exceptions import OCCSError
from content.models import Post, PostMediaAttachment, PostRevision

if TYPE_CHECKING:
    from accounts.models import User

#: A full snapshot every tenth revision. Ten because it bounds both costs at
#: something small: at most nine diffs to replay on a read, and at most nine
#: revisions over-retained by a prune that has to stop at a checkpoint.
CHECKPOINT_EVERY = 10


class RevisionNotFoundError(OCCSError):
    status_code = 404
    default_code = "revision_not_found"
    default_detail = "This post has no revision with that number."


class PostNotEditableError(OCCSError):
    status_code = 409
    default_code = "post_not_editable"
    default_detail = "This post cannot be edited in its current state."


#: Statuses where the content is already on its way out. Restoring under one of
#: these would either publish something nobody approved or rewrite the record
#: of what was actually sent.
UNEDITABLE_STATUSES = frozenset({"PUBLISHING", "PUBLISHED"})


def snapshot_of(post: Post) -> dict[str, Any]:
    """The post's content, as history records it.

    Deliberately *content* and not the whole row: `status`, `scheduled_at` and
    `delivery_mode` are lifecycle, owned by the schedule and approval services,
    and restoring an old body must not also un-schedule the post.

    **Reads the database, not the request's prefetch cache.** `Post.
    ordered_attachments()` is written to reuse whatever `PostViewSet` already
    prefetched, which is right for rendering and wrong here: by the time a
    snapshot is taken the caller has just *written* to that relation, and the
    cache still holds the row as it was. The `.order_by()` is what forces a
    fresh queryset — a bare `.all()` on a prefetched manager returns the cache.
    Without it, describing an image through the API records no revision at all,
    silently, because the before and after look identical.
    """
    attachments = [
        attachment
        for attachment in post.media_attachments.order_by("order", "id")
        if attachment.target_override_id is None
    ]
    return {
        "master_body": post.master_body,
        "media": [
            {
                "media_asset": attachment.media_asset_id,
                "order": attachment.order,
                "alt_text": attachment.alt_text,
            }
            for attachment in attachments
        ],
        "targets": [
            {
                "platform": target.platform,
                "body_override": target.body_override,
                "media_override": target.media_override,
                "platform_options": target.platform_options,
            }
            for target in post.targets.order_by("platform")
        ],
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """`{field: [before, after]}`, for the fields that actually moved.

    Whole-field rather than a structural diff of the nested lists: a media list
    is short, and a minimal edit script over it would be more code to get wrong
    than the bytes it saves.
    """
    return {key: [before.get(key), after[key]] for key in after if before.get(key) != after[key]}


def record(
    post: Post, *, author: User | None = None, reason: str = "", force: bool = False
) -> PostRevision | None:
    """Appends a revision if the content moved. Returns `None` when it did not.

    `force` is for the first revision of a post, which has nothing to compare
    against and must exist so that later diffs have an anchor.
    """
    if not flag_enabled(post.workspace.organization, CONTENT_MODEL_V2):
        # Part 3: flag off is pre-phase behaviour. Before Phase 1 an edit
        # recorded nothing, so it records nothing — the edit still happens.
        return None

    with transaction.atomic():
        # Locks the post, not the revision table: two concurrent saves of the
        # same post must not mint the same `sequence`, and two saves of
        # *different* posts should not wait on each other.
        Post.objects.select_for_update().get(pk=post.pk)
        latest = PostRevision.objects.filter(post=post).order_by("-sequence").first()
        after = snapshot_of(post)

        if latest is None:
            return PostRevision.objects.create(
                post=post,
                author=author,
                sequence=1,
                snapshot=after,
                diff={},
                is_checkpoint=True,
                reason=reason,
            )

        # `latest` is already the row `state_at` would look up by sequence —
        # reusing it here is what keeps every post save from re-querying a row
        # it is holding.
        before = _state_of(latest)
        delta = _diff(before, after)
        if not delta and not force:
            return None

        sequence = latest.sequence + 1
        is_checkpoint = sequence % CHECKPOINT_EVERY == 1
        return PostRevision.objects.create(
            post=post,
            author=author,
            sequence=sequence,
            snapshot=after if is_checkpoint else {},
            diff=delta,
            is_checkpoint=is_checkpoint,
            reason=reason,
        )


def _state_of(target: PostRevision) -> dict[str, Any]:
    """The post's content as of an **already-loaded** revision row.

    The reconstruction proper — `state_at` and `record` both need it, and only
    differ in whether they already hold the row or have to look it up by
    sequence. Split out so `record`'s hot path (every post save, alt-text
    edit, and platform-options save calls it once the post has a second
    revision) doesn't re-query the row it just fetched as `latest`.
    """
    anchor = (
        PostRevision.objects.filter(
            post_id=target.post_id, sequence__lte=target.sequence, is_checkpoint=True
        )
        .order_by("-sequence")
        .first()
    )
    if anchor is None:
        raise RevisionNotFoundError(
            "This revision's checkpoint has been pruned and it can no longer be reconstructed.",
            detail={"post": target.post_id, "sequence": target.sequence},
        )

    state = dict(anchor.snapshot)
    forward = PostRevision.objects.filter(
        post_id=target.post_id, sequence__gt=anchor.sequence, sequence__lte=target.sequence
    ).order_by("sequence")
    for revision in forward:
        for key, (_before, after) in revision.diff.items():
            state[key] = after
    return state


def state_at(post: Post, sequence: int) -> dict[str, Any]:
    """The post's content as of `sequence`.

    Walks forward from the nearest checkpoint at or before it. A history whose
    checkpoint has been pruned away is unreconstructable, which is precisely
    why `prune_expired` stops at a checkpoint rather than at a date.
    """
    target = PostRevision.objects.filter(post=post, sequence=sequence).first()
    if target is None:
        raise RevisionNotFoundError(detail={"post": post.pk, "sequence": sequence})
    return _state_of(target)


def restore(post: Post, *, sequence: int, author: User | None = None) -> Post:
    """Puts an old version back, as a new revision."""
    if post.status in UNEDITABLE_STATUSES:
        raise PostNotEditableError(
            f"A {post.get_status_display().lower()} post cannot be restored.",
            detail={"post": post.pk, "status": post.status},
        )
    # A restore rewrites the body, so it is a content mutation like any other
    # and the approval lock applies (P2-11). Reading history stays open — being
    # locked out of what a post used to say helps nobody.
    from workspaces.services.approvals import ensure_unlocked

    ensure_unlocked(post)

    state = state_at(post, sequence)

    with transaction.atomic():
        post.master_body = state["master_body"]
        post.save(update_fields=["master_body", "updated_at"])

        PostMediaAttachment.objects.filter(post=post).delete()
        PostMediaAttachment.objects.bulk_create(
            PostMediaAttachment(
                post=post,
                media_asset_id=item["media_asset"],
                order=item["order"],
                alt_text=item["alt_text"],
            )
            for item in state["media"]
        )

        by_platform = {target.platform: target for target in post.targets.all()}
        for item in state["targets"]:
            target = by_platform.get(item["platform"])
            if target is None:
                # The target was removed after this revision was taken.
                # Restoring content must not resurrect a publish destination
                # — that is the schedule service's decision, not history's.
                continue
            target.body_override = item["body_override"]
            target.media_override = item["media_override"]
            target.platform_options = item["platform_options"]
            target.save(
                update_fields=[
                    "body_override",
                    "media_override",
                    "platform_options",
                    "updated_at",
                ]
            )

    post.refresh_from_db()
    # Restoring changes the body, so annotations are re-checked here too — and
    # symmetrically: putting back the text an annotation was written about
    # re-attaches it (P2-02). Imported at call time rather than at module
    # scope: `collaboration.services` reads `content.models`, and `content
    # .services.posts` already imports this module.
    from collaboration.services import reanchor

    reanchor(post)
    record(post, author=author, reason=f"restored from revision {sequence}", force=True)
    return post


def prune_expired(*, now: Any = None) -> int:
    """Drops history past each organization's `Plan.version_history_days`.

    **Stops at a checkpoint, not at a date.** Deleting purely by cutoff would
    take the checkpoint that the surviving diffs are anchored to, leaving rows
    that exist and cannot be reconstructed — a history that is worse than no
    history, because it looks complete. The cost is over-retaining at most
    `CHECKPOINT_EVERY - 1` revisions per post, which is the right side to err
    on.

    **Zero means unset.** `version_history_days` defaults to 0 and no seeded
    plan has to set it; reading that as "retain nothing" would delete every
    workspace's history the first night this ran. Both `0` and `UNLIMITED`
    retain, and only a positive horizon prunes.

    A bulk delete rather than per-row, and that is the one thing here that
    touches an append-only table. Retention is not a correction — nothing is
    being restated, the rows are ageing out under a plan the customer bought —
    so it is a delete, not a compensating row. `AppendOnly.delete()` still
    refuses a single-row delete, which is what keeps an ad-hoc cleanup from
    borrowing this exception.
    """
    from django.utils import timezone

    from billing.models import UNLIMITED
    from workspaces.models import Organization

    moment = now or timezone.now()
    total = 0

    for organization in Organization.objects.select_related("plan"):
        plan = organization.plan
        days = plan.version_history_days if plan is not None else 0
        if days <= 0 or days == UNLIMITED:
            continue

        cutoff = moment - dt.timedelta(days=days)
        expired_post_ids = (
            PostRevision.objects.filter(
                post__workspace__organization=organization, created_at__lt=cutoff
            )
            .values_list("post_id", flat=True)
            .distinct()
        )
        for post_id in list(expired_post_ids):
            anchor = (
                PostRevision.objects.filter(
                    post_id=post_id, created_at__lt=cutoff, is_checkpoint=True
                )
                .order_by("-sequence")
                .first()
            )
            if anchor is None:
                # Nothing old enough is a checkpoint, so nothing can be dropped
                # without orphaning what stays.
                continue
            deleted, _ = PostRevision.objects.filter(
                post_id=post_id, sequence__lt=anchor.sequence
            ).delete()
            total += deleted

    return total
