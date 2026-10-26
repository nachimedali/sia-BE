"""The AI-generated label (S17): any picture or video with a person the model
drew carries it through to publishing, and nobody can remove it.

Three places hold the rule, and they read the same facts:

* the record — `VideoRender.labels`, and `Generation.creative["avatar"]` on a
  still made with an avatar, so the provenance is in the data, not in a flag;
* the draft — `with_disclosure` puts the label in the caption of every post the
  Studio sends (`video.send`, `variants.commit`), in the render's language;
* the gate — the pre-publish check `checks.services._check_disclosure` refuses
  (BLOCK, so scheduling is a 409) a post whose media has a drawn person and
  whose caption no longer says so.

The caption is where the label lives because it is the one surface every
platform publishes as written. A platform's own AI toggle, where one exists,
is a later addition on top, never a replacement.
"""

from __future__ import annotations

from typing import Any

AI_GENERATED = "ai_generated"

#: What the caption says, per content language (L-6). `en` is the fallback.
LABELS = {"en": "AI-generated", "fr": "Généré par IA", "it": "Generato con IA"}

#: Any of these in a caption counts as the label, whatever the case.
_MARKERS = (
    "ai-generated",
    "ai generated",
    "#aigenerated",
    "généré par ia",
    "genere par ia",
    "generato con ia",
    "generata con ia",
)

#: How far up `derived_from` a video's still is followed.
_DEPTH = 4


def labels_for(avatar: Any) -> list[str]:
    return [AI_GENERATED] if avatar is not None else []


def has_disclosure(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(marker in lowered for marker in _MARKERS)


def with_disclosure(text: str, labels: list[str] | None, *, language: str = "") -> str:
    if AI_GENERATED not in (labels or []) or has_disclosure(text):
        return text
    label = LABELS.get(language or "en", LABELS["en"])
    return f"{text.rstrip()}\n\n{label}" if text.strip() else label


def shows_drawn_person(asset: Any) -> bool:
    """A picture made with an avatar, or a video whose frames came from one."""
    from ai.models import VideoRender

    current, seen = asset, 0
    while current is not None and seen < _DEPTH:
        # Ids first: an uploaded picture with no generation and no render
        # behind it costs one query here, not three.
        if current.generation_id is not None and (current.generation.creative or {}).get("avatar"):
            return True
        if VideoRender.objects.filter(
            output_id=current.pk, labels__contains=[AI_GENERATED]
        ).exists():
            return True
        current = current.derived_from if current.derived_from_id is not None else None
        seen += 1
    return False
