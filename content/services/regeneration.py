"""Applying a regenerated result to a post (the post editor).

The second half of "Regenerate": `ai.services.regeneration.regenerate` starts a
new generation from the post's settings without touching the post; once it has
finished, `apply_generation` makes it the post's content — body and media
replaced in one revision, approval reset by the ordinary rule that an approved
post whose content changes goes back to review.
"""

from __future__ import annotations

from typing import Any

from accounts.models import User
from ai.models import Generation, GenerationStatus
from ai.services import regeneration as ai_regeneration
from ai.services.variants import post_body
from common.exceptions import StateConflict
from content.models import SLOTTED_STATUSES, Post
from content.services.posts import ensure_editable, update_post


class GenerationNotReadyError(StateConflict):
    default_code = "generation_not_ready"
    default_detail = "That generation has not finished."


class PostScheduledError(StateConflict):
    default_code = "post_scheduled"
    default_detail = "Unschedule this post before replacing its content."


def regenerate_post(
    post: Post,
    *,
    user: User,
    reasons: list[str] | None = None,
    note: str = "",
    creative: dict[str, Any] | None = None,
) -> Generation:
    """Start a regeneration — refused, like any content change, on a post that
    can no longer change. The generation itself is `ai`'s to create."""
    ensure_editable(post)
    return ai_regeneration.regenerate(
        post, user=user, reasons=reasons, note=note, creative=creative
    )


def apply_generation(post: Post, *, generation: Generation, author: User) -> Post:
    """Make a finished generation the post's content, as one revision.

    The top-ranked variant's copy becomes the body; every variant's image, in
    rank order, becomes the media. A scheduled post is refused rather than
    quietly re-armed with content nobody approved: unschedule, regenerate,
    approve, schedule.
    """
    ensure_editable(post)
    if post.status in SLOTTED_STATUSES:
        raise PostScheduledError(detail={"post": post.pk, "status": post.status})
    if generation.status != GenerationStatus.SUCCEEDED:
        raise GenerationNotReadyError(
            detail={"generation": generation.pk, "status": generation.status}
        )

    variants = list(generation.variants.select_related("media_asset").order_by("rank", "id"))
    fields: dict[str, Any] = {"generation": generation}
    body = post_body(variants[0]) if variants else ""
    if body:
        fields["master_body"] = body
    media = [variant.media_asset for variant in variants if variant.media_asset is not None]
    if media:
        fields["media_asset_ids"] = media
    update_post(post, author=author, reason="regenerated", **fields)
    return post
