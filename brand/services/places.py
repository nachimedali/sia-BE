"""The brand's own places, and the catalogue places it keeps (S17-28).

```
add      a photo of the brand's place → an `upload` place, ready at once
keep     a catalogue place → one empty-set plate, an image generation paid
         on pass, never made twice for one brand
resolve  a `made` place takes its picture from its generation once it passed
own      `own:<id>` → this brand's ready upload, or 404
plate    a catalogue key → this brand's ready kept plate, or None
```

Every lookup is scoped to the workspace, so another brand's place is absent
(rule 3). A plate is made by the image pipeline like any other picture —
`create_generation`, then `run_generation` on `ai_q` — so the gate, the debit
on pass and the record are the ones every image has.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from rest_framework.exceptions import NotFound, ValidationError

from ai.models import CreativeKind, CreativeOption, GenerationKind, GenerationMode, GenerationStatus
from billing.services.entitlements import entitlements_for
from brand.models import BrandPlace, PlaceSource
from content.models import MediaAsset, MediaKind
from workspaces.models import Workspace

#: How a template location names the brand's own place.
OWN = "own:"
#: Uploads a brand may hold — a bound on the list, not a plan number.
MAX_PLACES = 20
PLATE_ASPECT = "9:16"
PLATE_MODE = GenerationMode.IDEA


def keep_credits(workspace: Workspace) -> int:
    """What one plate costs: the image price of the mode it is made in."""
    from integrations.generation import get_generation

    return get_generation(workspace.organization).resolve_cost(
        kind=GenerationKind.IMAGE, mode=PLATE_MODE
    )


def resolve(place: BrandPlace) -> BrandPlace:
    """A `made` place whose generation passed takes its first picture."""
    if place.media_id is None and place.generation_id is not None:
        generation = place.generation
        if generation is not None and generation.status == GenerationStatus.SUCCEEDED:
            variant = generation.variants.filter(media_asset__isnull=False).order_by("id").first()
            if variant is not None:
                place.media = variant.media_asset
                place.save(update_fields=["media"])
    return place


def status_of(place: BrandPlace) -> str:
    """`ready`, `making` while its generation runs, or `failed`."""
    if place.media_id is not None:
        return "ready"
    generation = place.generation
    if generation is not None and generation.status == GenerationStatus.PENDING:
        return "making"
    return "failed"


def list_places(workspace: Workspace) -> list[BrandPlace]:
    return [
        resolve(place)
        for place in BrandPlace.objects.filter(workspace=workspace).select_related(
            "media", "generation"
        )
    ]


def add(workspace: Workspace, *, user: Any, media_id: Any, label: str) -> BrandPlace:
    """A photo of the brand's own place. Another brand's picture is a 404."""
    if not isinstance(media_id, int) or isinstance(media_id, bool):
        raise ValidationError({"media": "Pick the photo of your place."})
    media = MediaAsset.objects.filter(workspace=workspace, pk=media_id).first()
    if media is None:
        raise NotFound("That photo is not in this brand's library.")
    if media.kind != MediaKind.IMAGE:
        raise ValidationError({"media": "A place is a photo."})
    if BrandPlace.objects.filter(workspace=workspace, source=PlaceSource.UPLOAD).count() >= (
        MAX_PLACES
    ):
        raise ValidationError({"media": f"A brand keeps at most {MAX_PLACES} places."})
    return BrandPlace.objects.create(
        workspace=workspace,
        label=(label or "").strip()[:80] or "Our place",
        source=PlaceSource.UPLOAD,
        media=media,
        created_by=user,
    )


def plate_prompt(row: CreativeOption, workspace: Workspace) -> str:
    """An empty set for the place, room left where the product will stand."""
    from ai.services.ad_templates import Place, place_text

    meta = row.metadata or {}
    place = Place(
        set=str(meta.get("set") or row.prompt_fragment or row.key),
        light=str(meta.get("light") or ""),
        zone=str(meta.get("zone") or ""),
        forbid=str(meta.get("forbid") or ""),
    )
    scene = place_text(place.set, workspace)
    lines = [
        f"Vertical 9:16 photo of an empty set for a short social ad: {scene}.",
        f"Light: {place.light}." if place.light else "",
        "An empty set: no people, no product, no hands"
        + (
            f"; leave clear the space where a product will stand: {place.zone}."
            if place.zone
            else "."
        ),
        "Deep focus, true-to-life colour, real materials, no readable text.",
        f"Avoid: {place.forbid}." if place.forbid else "",
    ]
    return " ".join(line for line in lines if line)


def keep(workspace: Workspace, *, user: Any, place_key: str) -> tuple[BrandPlace, bool]:
    """The brand's plate for a catalogue place, made now if it has none.
    Returns the place and whether it was made by this call. A failed plate
    is replaced; a ready or running one is returned as it is, unpaid."""
    from ai.services.images import queue_image

    row = CreativeOption.objects.filter(
        kind=CreativeKind.PLACE, key=str(place_key or ""), is_active=True
    ).first()
    if row is None:
        raise ValidationError({"place": f"'{place_key}' is not a place."})
    existing = BrandPlace.objects.filter(
        workspace=workspace, source=PlaceSource.MADE, place_key=row.key
    ).first()
    if existing is not None:
        if status_of(resolve(existing)) != "failed":
            return existing, False
        existing.delete()
    entitlements_for(workspace).require_credits(keep_credits(workspace))
    try:
        place = BrandPlace.objects.create(
            workspace=workspace,
            label=row.localized("label"),
            source=PlaceSource.MADE,
            place_key=row.key,
            created_by=user,
        )
    except IntegrityError:
        # Two clicks raced: the other one is making it.
        return BrandPlace.objects.get(
            workspace=workspace, source=PlaceSource.MADE, place_key=row.key
        ), False
    place.generation = queue_image(
        workspace=workspace,
        user=user,
        mode=PLATE_MODE,
        prompt=plate_prompt(row, workspace),
        aspect=PLATE_ASPECT,
    )
    place.save(update_fields=["generation"])
    return resolve(place), True


def delete(workspace: Workspace, place_id: int) -> None:
    deleted, _ = BrandPlace.objects.filter(workspace=workspace, pk=place_id).delete()
    if not deleted:
        raise NotFound


def own(workspace: Workspace, location: str) -> BrandPlace:
    """`own:<id>` → this brand's ready upload; anything else here is a 404."""
    raw = location.removeprefix(OWN)
    place = (
        BrandPlace.objects.filter(
            workspace=workspace, source=PlaceSource.UPLOAD, pk=int(raw), media__isnull=False
        ).first()
        if raw.isdigit()
        else None
    )
    if place is None:
        raise NotFound("That place is not in this brand.")
    return place


def plate(workspace: Workspace, place_key: str) -> BrandPlace | None:
    """This brand's ready kept plate for a catalogue place, or None."""
    place = BrandPlace.objects.filter(
        workspace=workspace, source=PlaceSource.MADE, place_key=place_key
    ).first()
    if place is None:
        return None
    place = resolve(place)
    return place if place.media_id is not None else None


def kept_keys(workspace: Workspace) -> list[str]:
    """The catalogue places this brand has a ready plate for."""
    return [
        place.place_key
        for place in list_places(workspace)
        if place.source == PlaceSource.MADE and place.media_id is not None
    ]
