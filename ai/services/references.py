"""Composing from Playbook references (`GenerationMode.RECIPE`).

A person picks one to six public posts in the Playbook, says what they like in
each, and asks for their own product in that spirit. This module turns that into
what the models are told, following the goose-skills creative rules
(github.com/gooseworks-ai/goose-skills, vendored under `feed/vendor/`):

* `remix-graphic-ad-from-reference` — the reference lends its *structure*, the
  brand lends the product; every instance of the reference's product is
  replaced, and no source brand, logo or label survives anywhere in frame.
  Hand-held references hallucinate hands, so hands appear only when asked.
* `product-photoshoot` / `create-image-fal` — the product's silhouette,
  materials, colourway, logo and packaging stay exactly as photographed; the
  prompt describes the photograph, never the device; nothing is invented.
* `verify-product-image` — checked after, never trusted: the quality gate
  re-measures product identity, and adds a ceiling on how close the result may
  sit to any reference (`ai.services.quality`, `reference_distance`).

The references reach this module as a **snapshot** in `Generation.creative`
(written by `playbook.services.compose`, never by a client): a post deleted from
the Playbook tomorrow does not change what today's generation was asked.

**Two kinds of line.** `brief_lines` is what the text model may rewrite into
one art-director paragraph (`ai.services.rephrase.reference_brief`).
`rule_lines` is added after the rewrite, verbatim, so no model can soften the
rules about what may be taken from someone else's post.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ai.providers.reference_images import ReferenceImageError, get_reference_image_fetcher

logger = logging.getLogger(__name__)

MAX_REFERENCES = 6
NOTE_MAX = 280

#: What a person may take from a reference, and how the model is told. Stills
#: only: pacing and sound belong to a clip, which the Studio's Motion step
#: directs from the still this produces.
ASPECTS: dict[str, tuple[str, str]] = {
    "framing": ("Framing", "the framing and the camera's distance and angle"),
    "light": ("Light", "the light: its direction, softness and time of day"),
    "colours": ("Colours", "the colour palette"),
    "layout": ("Layout", "where things sit in the frame and how much space is left empty"),
    "setting": ("Setting", "the kind of setting, not the place itself"),
    "mood": ("Mood", "the mood"),
}
INFLUENCE: dict[str, str] = {
    "light": "a light nod",
    "balanced": "a clear influence",
    "strong": "follow it closely",
}


@dataclass(frozen=True)
class ReferenceImage:
    label: str
    content: bytes


def label(index: int) -> str:
    return f"R{index + 1}"


def _describe(ref: dict[str, Any]) -> str:
    kind = {"SHORT": "short video", "VIDEO": "video", "SEARCH": "search rising today"}.get(
        str(ref.get("format")), "post"
    )
    source = {"youtube": "YouTube", "google_trends": "Google"}.get(str(ref.get("provider")), "")
    by = f" by {ref['creator']}" if ref.get("creator") else ""
    return f"{source} {kind} “{ref.get('title', '')}”{by}".strip()


def brief_lines(references: list[dict[str, Any]]) -> list[str]:
    """One line per reference: what to take, how strongly, and the person's
    own words. Notes are quoted as what the person likes — data, never
    instructions (the system prompt says so too)."""
    lines: list[str] = []
    for index, ref in enumerate(references):
        if ref.get("format") == "SEARCH":
            lines.append(
                f"{label(index)} is a {_describe(ref)}: use it only as a topical cue for the "
                "occasion or setting, and only if it fits the product naturally."
            )
            continue
        takes = [ASPECTS[key][1] for key in ref.get("take", []) if key in ASPECTS]
        what = "; ".join(takes) if takes else "its overall composition and light"
        weight = INFLUENCE.get(str(ref.get("influence")), INFLUENCE["balanced"])
        line = f"{label(index)} ({_describe(ref)}), {weight}: take {what}."
        note = str(ref.get("note") or "").strip()
        if note:
            line += f' The user likes: "{note[:NOTE_MAX]}".'
        lines.append(line)
    return lines


def rule_lines(
    references: list[dict[str, Any]], *, product_images: int, seen: list[str]
) -> list[str]:
    """The rules, verbatim, and which attached picture is which.

    The image model reads attachments in order, so the prompt numbers them: the
    product's photographs first (the thing to reproduce), then the references
    it may learn from (the things it must not reproduce)."""
    lines: list[str] = []
    order: list[str] = []
    if product_images:
        order.append(
            f"images 1 to {product_images} are photographs of the product: reproduce the "
            "product exactly as shown"
            if product_images > 1
            else "image 1 is a photograph of the product: reproduce the product exactly as shown"
        )
    if seen:
        first = product_images + 1
        last = product_images + len(seen)
        if first == last:
            order.append(f"image {first} is a composition reference ({seen[0]})")
        else:
            order.append(
                f"images {first} to {last} are composition references "
                f"({', '.join(seen)}), in that order"
            )
    if order:
        lines.append("Attached, in order: " + "; ".join(order) + ".")
    if references:
        lines.append(
            "From the references take only the qualities named for each. Never reproduce "
            "their subject, people, faces, hands, products, brands, logos, labels, packaging, "
            "captions, on-screen text, watermarks or recognisable places. The result is a new "
            "photograph of this product alone, not an edit or a collage of a reference."
        )
        missing = [
            label(i)
            for i, ref in enumerate(references)
            if ref.get("format") != "SEARCH" and label(i) not in seen
        ]
        if missing:
            lines.append(
                f"No picture is attached for {', '.join(missing)}: follow the description only."
            )
        lines.append("Do not depict real people, news events or other brands from any reference.")
    return lines


def images(references: list[dict[str, Any]]) -> list[ReferenceImage]:
    """The references' pictures, read once per generation and never stored.

    Only references whose post lends a picture (`image: true`, decided by the
    Playbook from the post's kind) are read; one that cannot be read is
    skipped and logged, and `rule_lines` then says so to the model."""
    fetcher = get_reference_image_fetcher()
    out: list[ReferenceImage] = []
    for index, ref in enumerate(references[:MAX_REFERENCES]):
        url = str(ref.get("thumbnail_url") or "")
        if not ref.get("image") or not url:
            continue
        try:
            out.append(ReferenceImage(label=label(index), content=fetcher.fetch(url)))
        except ReferenceImageError:
            logger.warning("reference picture unavailable", extra={"reference": label(index)})
    return out
