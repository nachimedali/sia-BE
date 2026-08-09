"""Revisions (design.md §8.3, §7): `POST /ai/generations/{id}/revise/`.

A revision is a child `Generation` with `mode=REVISION`, run through the same
`pipeline.run_generation` as any other generation — the cheaper price (§4.2:
1 credit vs 3 for a fresh Studio image) comes from a `GenerationCost` row
seeded for `(kind, REVISION)`, not a second code path or a hardcoded literal
(I8). This is why `GenerationMode.REVISION` is in `pipeline.ALLOWED_MODES`
but excluded from `DIRECTLY_CREATABLE_MODES` — only this module may set it,
and only against a generation that actually succeeded.
"""

from __future__ import annotations

from typing import Any

from ai.models import Generation, GenerationMode, GenerationStatus
from ai.services.costing import resolve_cost
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError


class RevisionNotAllowedError(OCCSError):
    default_code = "revision_not_allowed"
    default_detail = "Only a succeeded generation can be revised."


def create_revision(*, parent: Generation, user: Any, instructions: str) -> Generation:
    if parent.status != GenerationStatus.SUCCEEDED:
        raise RevisionNotAllowedError(detail={"generation": parent.pk, "status": parent.status})

    # Bypasses `pipeline.create_generation`'s mode check on purpose — this is
    # the one caller allowed to set REVISION, and it validates its own
    # precondition (a succeeded parent) instead.
    cost = resolve_cost(kind=parent.kind, mode=GenerationMode.REVISION)
    entitlements_for(parent.workspace).require_credits(cost)

    return Generation.objects.create(
        workspace=parent.workspace,
        user=user,
        kind=parent.kind,
        mode=GenerationMode.REVISION,
        prompt=instructions,
        product=parent.product,
        category=parent.category,
        voice_profile=parent.voice_profile,
        parent_generation=parent,
        aspect=parent.aspect,
        render_style=parent.render_style,
        scene=parent.scene,
        is_batch=parent.is_batch,
    )
