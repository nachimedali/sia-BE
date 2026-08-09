"""Post authoring (implementation.md §4.1: business logic in services/, never
in serializers or views; a Celery task body is a call into this module too).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from accounts.models import User
from categories.models import Category
from content.models import MediaAsset, Post, PostMediaAttachment
from workspaces.models import Workspace


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


def update_post(post: Post, **fields: Any) -> Post:
    """`fields` is exactly what the caller wants to change — a PATCH that
    omits `media_asset_ids` must not touch attachment order, so the view only
    passes keys that were actually present in the request body.
    """
    media_assets = fields.pop("media_asset_ids", None)

    for name, value in fields.items():
        setattr(post, name, value)
    if fields:
        post.save(update_fields=[*fields.keys(), "updated_at"])

    if media_assets is not None:
        _replace_media(post, media_assets)

    return post
