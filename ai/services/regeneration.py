"""Regenerating a post from the settings it was performed with (the post editor).

Lives in `ai/` because it *creates a generation* — the same job as the Studio's
`GenerateView`, with the same rule: only the DB half runs in the request, the
provider half goes to `ai_q`. Applying the finished result to the post is
`content.services.regeneration.apply_generation`; nothing here touches the post.

The post's generation is the starting point: same product, kind, voice and
creative settings, with whatever the user changed laid over them and the
reasons they gave appended to the brief. Reasons are `CreativeOption` rows of
kind `revise_reason` (rule 18): a new reason is an admin edit, and each carries
the `prompt_fragment` the model reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rest_framework.exceptions import ValidationError

from ai.models import CreativeKind, Generation, GenerationMode
from ai.services import creative as creative_service
from ai.services.pipeline import DIRECTLY_CREATABLE_MODES, create_generation
from ai.tasks import run_generation_task
from common.exceptions import StateConflict

if TYPE_CHECKING:
    from accounts.models import User
    from content.models import Post

#: A carousel regenerates as a carousel, up to this many images.
MAX_IMAGES = 4


class NotGeneratedError(StateConflict):
    default_code = "not_generated"
    default_detail = "This post was not generated, so there are no settings to regenerate from."


def _fragments(keys: list[str]) -> list[str]:
    rows = creative_service._active(CreativeKind.REVISE_REASON)
    unknown = sorted(set(keys) - rows.keys())
    if unknown:
        raise ValidationError({"reasons": f"Unknown reason: {', '.join(unknown)}."})
    return [rows[key].prompt_fragment or rows[key].label for key in keys]


def regenerate(
    post: Post,
    *,
    user: User,
    reasons: list[str] | None = None,
    note: str = "",
    creative: dict[str, Any] | None = None,
) -> Generation:
    """Start a new generation from this post's settings. Returns it `PENDING`
    (or finished, under eager Celery); the post is not touched. The caller
    checks the post is editable — that is the post's rule, not generation's."""
    parent = post.generation
    if parent is None:
        raise NotGeneratedError(detail={"post": post.pk})

    changes = [change for change in (*_fragments(reasons or []), note.strip()) if change]
    prompt = parent.prompt
    if changes:
        prompt = f"{prompt}\n\nChange from the previous version: {'; '.join(changes)}."

    mode = parent.mode
    if mode not in DIRECTLY_CREATABLE_MODES:
        mode = GenerationMode.PRODUCT if parent.product_id else GenerationMode.IDEA

    generation = create_generation(
        workspace=post.workspace,
        user=user,
        kind=parent.kind,
        mode=mode,
        prompt=prompt,
        product=parent.product,
        voice_profile=parent.voice_profile,
        aspect=parent.aspect,
        render_style=parent.render_style,
        scene=parent.scene,
        creative=creative_service.normalize({**parent.creative, **(creative or {})}),
    )
    Generation.objects.filter(pk=generation.pk).update(parent_generation=parent)

    images = post.media_assets.count()
    run_generation_task.delay(generation_id=generation.id, n=max(1, min(MAX_IMAGES, images)))
    generation.refresh_from_db()  # under eager Celery the task has already run
    return generation
