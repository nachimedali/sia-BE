"""One picture, queued (S17-28, S17-29): the entry point other apps use to have
the image pipeline make something that is not a Studio run — a brand's kept
place, a product's clean photo — without reaching into the pipeline itself
(`integrations/tests/test_seam_is_not_bypassed.py`). The pipeline gates and
debits it like every other image: on pass only.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from ai.models import Generation, GenerationKind
from ai.services import pipeline
from common.exceptions import ProviderError


def queue_image(
    *,
    workspace: Any,
    user: Any,
    mode: str,
    prompt: str,
    aspect: str,
    product: Any = None,
    source_media: Any = None,
) -> Generation:
    """A PENDING image generation, queued on `ai_q` (run inline under eager
    Celery), returned as it stands after the queueing."""
    from ai.tasks import run_generation_task

    generation = pipeline.create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=mode,
        prompt=prompt,
        product=product,
        aspect=aspect,
        source_media=source_media,
    )
    # Only an inline (eager) run raises here; the task has already marked the
    # row FAILED and logged the vendor's refusal. The caller reads the row.
    with suppress(ProviderError):
        run_generation_task.delay(generation_id=generation.id, n=1)
    generation.refresh_from_db()
    return generation
