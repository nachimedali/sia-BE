"""Post authoring (implementation.md §4.1: business logic in services/, never
in serializers or views; a Celery task body is a call into this module too).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from accounts.models import User
from categories.models import Category
from content.models import MediaAsset, Post, PostMediaAttachment, PostStatus
from workspaces.models import Workspace
from workspaces.services import approvals


def _replace_media(post: Post, media_assets: Sequence[MediaAsset]) -> None:
    """Full replace, not a diff: a composer sends the whole ordered list on
    every save, so reconciling an add/remove/reorder delta would be solving a
    problem nobody has yet."""
    PostMediaAttachment.objects.filter(post=post).delete()
    PostMediaAttachment.objects.bulk_create(
        PostMediaAttachment(post=post, media_asset=asset, order=index)
        for index, asset in enumerate(media_assets)
    )


def create_post(
    *,
    workspace: Workspace,
    author: User,
    master_body: str = "",
    category: Category | None = None,
    media_assets: Sequence[MediaAsset] = (),
) -> Post:
    post = Post.objects.create(
        workspace=workspace, author=author, master_body=master_body, category=category
    )
    if media_assets:
        _replace_media(post, media_assets)
    return post


#: Fields whose change means "the content changed", not just the metadata
#: around it. Deliberately narrow: `source`/`product`/`generation`/`category`
#: are all written by other phases' services (autopilot, repurposing) through
#: this same function, and none of them should silently undo an approval.
_CONTENT_FIELDS = frozenset({"master_body", "media_asset_ids"})


def update_post(post: Post, **fields: Any) -> Post:
    """`fields` is exactly what the caller wants to change — a PATCH that
    omits `media_asset_ids` must not touch attachment order, so the view only
    passes keys that were actually present in the request body.

    **Editing an `APPROVED` post reverts it to `PENDING_REVIEW`** (design.md
    §8.8) — approval attaches to content, not to the record, so a change to
    what would actually publish voids it regardless of whether the workspace's
    approval workflow is switched on right now. Only a content change does
    this: `product`/`generation`/`source`/`category` are metadata other
    phases' services write through this same function, and none of them is
    the thing an approver signed off on.
    """
    touches_content = bool(_CONTENT_FIELDS & fields.keys())
    media_assets = fields.pop("media_asset_ids", None)

    for name, value in fields.items():
        setattr(post, name, value)

    update_fields = [*fields.keys()]
    reverted = touches_content and post.status == PostStatus.APPROVED
    if reverted:
        post.status = PostStatus.PENDING_REVIEW
        update_fields.append("status")

    if update_fields:
        post.save(update_fields=[*update_fields, "updated_at"])

    if media_assets is not None:
        _replace_media(post, media_assets)

    if reverted:
        approvals.log(
            workspace=post.workspace,
            verb="post.edited_after_approval",
            target_repr=str(post),
            meta={"post": post.pk},
        )

    return post
