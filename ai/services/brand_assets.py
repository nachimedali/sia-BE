"""Brand assets: the person's own pictures attached to an image generation.

A logo, the packaging, a label, a storefront — anything the render must carry
that is not a photograph of the product itself. Three rules shape this module:

* **Not a product reference.** The product's photographs are what the quality
  gate holds identity to. An asset is something the render *includes*; scoring
  identity against a logo would fail every render that shows the product. So
  assets reach the provider and never the gate (`pipeline._attempt_image`).
* **Numbered last.** The provider receives the product's photographs, then a
  Playbook composition's references, then the assets. The composition's rule
  lines already number its pictures from the product's count, so appending
  here shifts nothing that is already said.
* **Stored as a snapshot.** `Generation.creative["assets"]` keeps the media ids
  with their role and note, so a regeneration carries them over and the record
  says which pictures shaped a render. Ownership is checked at the service
  boundary (`ensure_owned`), which the regeneration path goes through too.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

from common.exceptions import OCCSError
from content.models import MediaAsset, MediaKind

MAX_ASSETS = 3
NOTE_MAX = 140

#: What each role tells the model. The role is the person's statement of what
#: the picture is; the wording is ours, so it stays consistent across renders.
ROLES: dict[str, str] = {
    "logo": (
        "Image {n} is the brand's logo. Wherever a logo appears, use exactly this one — "
        "unaltered, undistorted and legible. Do not invent or add any other logo."
    ),
    "packaging": (
        "Image {n} is the product's packaging. When the package is in the scene, show this "
        "one as it is: same shape, colours and print."
    ),
    "other": "Image {n} is a brand reference to include as it is shown.",
}

#: Roles only the server attaches. `person` is an avatar's portrait (S17): it
#: is added by `ai.services.ad_templates` and the generate view after the
#: avatar has been checked — usable, in the plan, consent standing — so a
#: client can never name any picture a person and skip those checks.
SERVER_ROLES: dict[str, str] = {
    "person": (
        "Image {n} is the person in the scene. Keep this exact face, hair, skin tone and "
        "build; do not change who they are. They are an invented or consenting brand "
        "presenter, not a public figure."
    ),
}


class InvalidBrandAssetError(OCCSError):
    default_code = "invalid_brand_asset"
    default_detail = "This picture cannot be used as a brand asset."


ALL_ROLES = {**ROLES, **SERVER_ROLES}


def clean(raw: Any) -> list[dict[str, Any]]:
    """Structural validation only — the shape, the roles, the cap. Whether
    each picture belongs to the caller is `ensure_owned`'s question, asked
    where the workspace is known."""
    if not isinstance(raw, list):
        raise ValidationError({"assets": "Expected a list of brand assets."})
    if len(raw) > MAX_ASSETS:
        raise ValidationError({"assets": f"At most {MAX_ASSETS} brand assets per generation."})
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError({"assets": "Each brand asset is an object."})
        media, role, note = item.get("media"), item.get("role"), item.get("note", "")
        if not isinstance(media, int) or isinstance(media, bool):
            raise ValidationError({"assets": "Each brand asset names a media id."})
        if role not in ROLES:
            raise ValidationError({"assets": f"'{role}' is not a brand asset role."})
        if not isinstance(note, str) or len(note) > NOTE_MAX:
            raise ValidationError({"assets": f"A note is text of at most {NOTE_MAX} characters."})
        out.append({"media": media, "role": role, "note": note.strip()})
    if len({item["media"] for item in out}) != len(out):
        raise ValidationError({"assets": "The same picture is attached twice."})
    return out


def ensure_owned(workspace: Any, kind: str, assets: list[dict[str, Any]]) -> None:
    """Every asset is an image in this workspace, on an image generation. A
    picture from another brand — sibling or stranger — reads as absent, the
    same answer a scoped primary-key field gives."""
    if not assets:
        return
    from ai.models import GenerationKind

    if kind != GenerationKind.IMAGE:
        raise InvalidBrandAssetError(
            "Brand assets steer images; this generation writes text.", detail={"kind": kind}
        )
    ids = [item["media"] for item in assets]
    found = dict(
        MediaAsset.objects.filter(workspace=workspace, pk__in=ids).values_list("pk", "kind")
    )
    missing = [pk for pk in ids if pk not in found]
    if missing:
        raise InvalidBrandAssetError(
            "A brand asset is not one of this brand's pictures.", detail={"media": missing}
        )
    not_images = [pk for pk in ids if found[pk] != MediaKind.IMAGE]
    if not_images:
        raise InvalidBrandAssetError(
            "A brand asset must be an image.", detail={"media": not_images}
        )


def load(assets: list[dict[str, Any]]) -> list[tuple[dict[str, Any], bytes]]:
    """Each asset with its picture, in the order the prompt numbers them. One
    deleted since the request is dropped from *both* halves, so the numbering
    of what remains still matches the pictures sent."""
    by_id = MediaAsset.objects.in_bulk([item["media"] for item in assets])
    loaded = []
    for item in assets:
        asset = by_id.get(item["media"])
        if asset is None:
            continue
        with asset.file.open("rb") as handle:
            loaded.append((item, handle.read()))
    return loaded


def rule_lines(assets: list[dict[str, Any]], *, first: int) -> list[str]:
    """One line per asset, numbered from `first` — the position after every
    picture the provider receives before them."""
    lines = []
    for offset, item in enumerate(assets):
        line = ALL_ROLES[item["role"]].format(n=first + offset)
        if item["note"]:
            line = f"{line} Instruction: {item['note']}"
        lines.append(line)
    return lines
