"""Compose from references: the Playbook's hand-off to the generation pipeline.

The person picked posts on the wall, said what they like in each, chose a
product and a piece. This module checks that against what exists — the posts
are still servable, the product is this workspace's, the choices are rows in
the live catalog — snapshots the references, and asks the pipeline for a
`RECIPE` generation. It decides nothing about pixels.

**The snapshot is built here, from our own rows.** A client sends post ids; the
URLs the pipeline will read come from `PlaybookPost`, never from the request,
so nobody can point the server at an address of their choosing.

**Video is a still first.** A clip in the Studio animates a still
(`ai.services.video`), so a video composition renders 9:16 stills here and the
review offers "Animate" on each — through the same priced, reviewed video path
every other clip takes, rather than a second one.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound

from ai.models import CreativeKind, CreativeOption, Generation, GenerationKind
from ai.services import creative as creative_service
from ai.services import pipeline, references
from common.exceptions import OCCSError
from playbook.services import query
from products.models import Product
from workspaces.models import Workspace

VIDEO_ASPECT = "9:16"


class ReferenceUnavailableError(OCCSError):
    default_code = "reference_unavailable"
    default_detail = "A reference is no longer available."


def _video_format() -> str:
    row = (
        CreativeOption.objects.filter(
            kind=CreativeKind.FORMAT,
            is_active=True,
            metadata__kind=GenerationKind.IMAGE,
            metadata__aspect=VIDEO_ASPECT,
        )
        .order_by("sort_order", "pk")
        .first()
    )
    if row is None:
        raise OCCSError(
            "The 9:16 still format is missing from the catalogue.",
            code="format_not_configured",
            detail={"aspect": VIDEO_ASPECT},
        )
    return row.key


def snapshot(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What each reference was when it was used, for the pipeline and for the
    record. Raises with every unavailable id at once, so the page can mark them
    all rather than one per attempt."""
    rows = {pick["post"]: query.post(pick["post"]) for pick in picks}
    gone = [pk for pk, row in rows.items() if row is None]
    if gone:
        raise ReferenceUnavailableError(
            "Some references are no longer available. Remove them and try again.",
            detail={"posts": gone},
        )
    out: list[dict[str, Any]] = []
    for pick in picks:
        row = rows[pick["post"]]
        assert row is not None
        out.append(
            {
                "post": row.pk,
                "provider": row.provider,
                "format": row.format,
                "title": row.title,
                "creator": row.creator,
                "url": row.url,
                "thumbnail_url": row.thumbnail_url,
                "image": row.can_guide_pictures,
                "structure": list(row.structure or [])[:3],
                "note": str(pick.get("note") or "").strip()[: references.NOTE_MAX],
                "take": [key for key in pick.get("take", []) if key in references.ASPECTS],
                "influence": pick.get("influence") or "balanced",
            }
        )
    return out


def compose(*, workspace: Workspace, user: Any, data: dict[str, Any]) -> Generation:
    product = Product.objects.filter(workspace=workspace, pk=data["product"]).first()
    if product is None:
        # Another tenant's product is not a validation error; it does not
        # exist here (rule 3).
        raise NotFound("That product is not in this workspace.")

    output = data["output"]
    chosen: dict[str, Any] = {
        "format": _video_format() if output == "video" else data.get("format") or "",
        "language": data.get("language") or "",
        "moods": data.get("moods") or [],
        "platforms": data.get("platforms") or [],
    }
    creative = creative_service.normalize({key: value for key, value in chosen.items() if value})

    from ai.tasks import run_generation_task

    generation = pipeline.create_recipe_generation(
        workspace=workspace,
        user=user,
        product=product,
        prompt=str(data.get("brief") or "").strip(),
        creative={**creative, "output": output},
        references_snapshot=snapshot(data["references"]),
        paid_slots=int(data["variants"]),
    )
    run_generation_task.delay(generation_id=generation.id, n=int(data["variants"]))
    # Under eager Celery (tests, e2e) the task has already run.
    generation.refresh_from_db()
    return generation
