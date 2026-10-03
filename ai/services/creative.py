"""The Studio's creative controls, as the pipeline sees them.

`CreativeOption` rows are the vocabulary; this module is the only place that
turns a user's selection into something the rest of the system acts on. Three
jobs, deliberately in one file so a new control is one edit and not three that
drift:

* **`normalize`** — validate a selection against the live catalog. An unknown,
  inactive or mistyped choice is a 400 naming the field. The alternative — to
  ignore what we do not recognise — is a control that appears to work and
  changes nothing, which is the worst way for a creative tool to fail.
* **`image_lines` / `caption_lines`** — what the models are told. Each choice
  contributes the row's `prompt_fragment`, so the wording of a scene lives with
  the scene and an operator who edits it changes what is asked.
* **`style_for`** — how the *fake* image provider tints its mock-up, combined
  from the rows' `metadata.grade`. Real providers take their direction from
  the prompt and ignore it.

Selections are stored by key. A label edited in admin never rewrites what an
old generation was asked for.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

from ai.models import CreativeKind, CreativeOption
from content.models import Platform

#: Controls that hold exactly one choice.
SINGLE = (
    CreativeKind.SCENE,
    CreativeKind.LIGHT,
    CreativeKind.CAMERA,
    CreativeKind.CAST,
    CreativeKind.VIBE,
    CreativeKind.PALETTE,
    CreativeKind.LANGUAGE,
    CreativeKind.CTA,
    CreativeKind.FORMAT,
    CreativeKind.TEMPO,
    CreativeKind.DYNAMICS,
    CreativeKind.TONE,
)
#: Request key -> the kind it holds several of. Only moods are multi-select.
MULTI = {"moods": CreativeKind.MOOD}

AVOID_MAX = 200
#: Where a control's `prompt_fragment` is placed, and under what lead-in. Order
#: is the order the model reads them in: place, light, framing, then feel.
_IMAGE_LEADS: tuple[tuple[str, str], ...] = (
    (CreativeKind.SCENE, "Setting"),
    (CreativeKind.LIGHT, "Lighting"),
    (CreativeKind.CAMERA, "Camera"),
    (CreativeKind.CAST, "People"),
    (CreativeKind.VIBE, "Styling"),
    (CreativeKind.PALETTE, "Colour palette"),
    (CreativeKind.DYNAMICS, "Intensity"),
    (CreativeKind.TEMPO, "Variation"),
)


def _active(kind: str) -> dict[str, CreativeOption]:
    return {row.key: row for row in CreativeOption.objects.filter(kind=kind, is_active=True)}


def catalog() -> dict[str, list[CreativeOption]]:
    """Every active row, grouped by kind and in display order — **including
    kinds with no rows**, so a client never has to ask whether a key exists."""
    grouped: dict[str, list[CreativeOption]] = {kind.value: [] for kind in CreativeKind}
    for row in CreativeOption.objects.filter(is_active=True):
        grouped[row.kind].append(row)
    return grouped


def normalize(raw: Any) -> dict[str, Any]:
    """Validate a selection against the catalog and return only what is valid.

    Raises one `ValidationError` carrying every problem, so a client fixing a
    form sees them all at once rather than one per round trip.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError({"creative": "Expected an object."})

    errors: dict[str, str] = {}
    out: dict[str, Any] = {}
    known = {kind.value for kind in SINGLE} | set(MULTI) | {"toggles", "avoid", "platforms"}
    for key in raw:
        if key not in known:
            errors[key] = "Unknown creative control."

    for kind in SINGLE:
        if kind not in raw:
            continue
        value = raw[kind]
        if not isinstance(value, str) or value not in _active(kind):
            errors[kind] = f"'{value}' is not an available {kind}."
            continue
        out[kind] = value

    for field, kind in MULTI.items():
        if field not in raw:
            continue
        values = raw[field]
        active = _active(kind)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            errors[field] = "Expected a list of keys."
        elif bad := [v for v in values if v not in active]:
            errors[field] = f"Not available: {', '.join(bad)}."
        else:
            out[field] = list(dict.fromkeys(values))

    if "toggles" in raw:
        toggles = raw["toggles"]
        active = _active(CreativeKind.TOGGLE)
        if not isinstance(toggles, dict) or not all(isinstance(v, bool) for v in toggles.values()):
            errors["toggles"] = "Expected an object of true/false values."
        elif bad := [t for t in toggles if t not in active]:
            errors["toggles"] = f"Unknown option: {', '.join(bad)}."
        else:
            out["toggles"] = dict(toggles)

    if "avoid" in raw:
        avoid = raw["avoid"]
        if not isinstance(avoid, str) or len(avoid) > AVOID_MAX:
            errors["avoid"] = f"Text of at most {AVOID_MAX} characters."
        else:
            out["avoid"] = avoid.strip()

    if "platforms" in raw:
        platforms = raw["platforms"]
        if not isinstance(platforms, list) or not all(
            isinstance(p, str) and p in Platform.values for p in platforms
        ):
            errors["platforms"] = "Expected a list of known platforms."
        else:
            out["platforms"] = list(dict.fromkeys(platforms))

    if errors:
        raise ValidationError({"creative": errors})
    return out


def _chosen(creative: dict[str, Any], kind: str) -> CreativeOption | None:
    key = creative.get(kind)
    if not key:
        return None
    return CreativeOption.objects.filter(kind=kind, key=key).first()


def _moods(creative: dict[str, Any]) -> list[CreativeOption]:
    keys = creative.get("moods") or []
    rows = {r.key: r for r in CreativeOption.objects.filter(kind=CreativeKind.MOOD, key__in=keys)}
    return [rows[k] for k in keys if k in rows]


def image_lines(creative: dict[str, Any]) -> list[str]:
    """What the image model is told about the brief's look and feel."""
    lines: list[str] = []
    for kind, lead in _IMAGE_LEADS:
        row = _chosen(creative, kind)
        if row and row.prompt_fragment:
            lines.append(f"{lead}: {row.prompt_fragment}.")
    moods = [m.prompt_fragment for m in _moods(creative) if m.prompt_fragment]
    if moods:
        lines.append("Mood: " + "; ".join(moods) + ".")
    avoid = (creative.get("avoid") or "").strip()
    if avoid:
        lines.append(f"Keep out of the frame: {avoid}.")
    return lines


def caption_lines(creative: dict[str, Any]) -> list[str]:
    """What the copywriter is told: language, voice, the ask, and the shot."""
    lines: list[str] = []
    for kind in (CreativeKind.LANGUAGE, CreativeKind.TONE, CreativeKind.CTA):
        row = _chosen(creative, kind)
        if row and row.prompt_fragment:
            lines.append(row.prompt_fragment)
    scene = _chosen(creative, CreativeKind.SCENE)
    if scene and scene.prompt_fragment:
        lines.append(f"The picture is set {scene.prompt_fragment}.")
    toggles = creative.get("toggles") or {}
    headline = CreativeOption.objects.filter(
        kind=CreativeKind.TOGGLE, key="headline_on_image"
    ).first()
    if toggles.get("headline_on_image", True) and headline and headline.prompt_fragment:
        lines.append(headline.prompt_fragment)
    avoid = (creative.get("avoid") or "").strip()
    if avoid:
        lines.append(f"Do not mention or imply: {avoid}.")
    return lines


def style_for(creative: dict[str, Any]) -> dict[str, float] | None:
    """The combined colour grade of every grade-bearing choice, or `None` when
    nothing was chosen. Chroma multiplies; the Cb/Cr shifts add — the same
    algebra the fake applies, so two choices that each warm the picture warm it
    twice as much, as they would in a real grade."""
    if not creative:
        return None
    rows: list[CreativeOption] = [
        row
        for kind in (CreativeKind.LIGHT, CreativeKind.PALETTE, CreativeKind.DYNAMICS)
        if (row := _chosen(creative, kind)) is not None
    ] + _moods(creative)
    chroma, cb, cr = 1.0, 0.0, 0.0
    for row in rows:
        grade = row.metadata.get("grade") or {}
        chroma *= float(grade.get("chroma", 1.0))
        cb += float(grade.get("cb", 0))
        cr += float(grade.get("cr", 0))
    return {"chroma": round(chroma, 4), "cb": cb, "cr": cr}


def wants_copy(creative: dict[str, Any]) -> bool:
    """Studio runs set `creative`; nobody else does. That is the switch for
    writing a headline and caption alongside each image — and the reason
    autopilot's image runs cost and behave exactly as they did."""
    return bool(creative)


def render_style_label(creative: dict[str, Any]) -> str:
    """The legacy `render_style` column, kept populated: the moods' labels."""
    return ", ".join(m.label for m in _moods(creative))


def scene_label(creative: dict[str, Any]) -> str:
    row = _chosen(creative, CreativeKind.SCENE)
    return row.label if row else ""
