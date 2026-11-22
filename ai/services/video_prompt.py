"""Prompt compiler v2 for video templates (S19, `upgrade_rendering.md` §5 to §6).

```
direction   a template's timed scene: shot, camera, beats or shots, sound, avoid
scaled      its beats moved to the render's length; the hook keeps its window
compile     FORMAT · CAMERA · SUBJECT · ACTION · SCENE · LIGHT · STYLE · SOUND · AVOID,
            in the model profile's timecode syntax and word budget
```

Only with `pro_rendering_s19` on, and only for a template whose row carries a
`direction` (`ai/0051` loads the 67 rewritten scenes into rows still holding the
seed). Everything else renders today's prompt, byte for byte.

**The budget trims description, never instruction.** Over a profile's word
budget, STYLE goes first, then SCENE, SOUND and LIGHT, a clause at a time from
the end. FORMAT, CAMERA, SUBJECT, ACTION and AVOID are never cut: a prompt that
loses what happens or what must not is worse than a long one.

**Words the model must not be asked for.** Zooms, speed changes, frame-rate
tricks, split screens and cuts are the composer's (S19-06); `lint` finds them in
a scene's model-facing text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from rest_framework.exceptions import NotFound

from ai.ad_template_seed import CAPTURES
from ai.models import AITask, CreativeKind, CreativeOption, VideoModelProfile
from billing.services.flags import PRO_RENDERING_S19, flag_enabled
from common.exceptions import OCCSError

ROUTES = ("F", "L", "FL", "2S", "3S", "4S", "Reel")
#: Shot uses a direction may name; `end` is the composer's end card.
DIRECTION_USES = ("frame", "photo", "place", "screen", "card", "cutout", "grid", "chat", "end")
#: What a scene can need from the model route (S19-09): landing on a last
#: frame, its own sound, two or more people, a first and a last real photo.
REQUIREMENTS = ("L", "AU", "MP", "FL")

#: The video model's AVOID (§6.1, the scene files' global list). Not the
#: template's `negative`: that is the first frame's (image) negative, a list of
#: finishes the frame already shows, and at ~90 words it would fill the budget
#: before the scene was described. The template negative keeps steering the
#: still; the clip is told what goes wrong in motion.
GLOBAL_AVOID = (
    "no text, captions or subtitles",
    "the label, shape and colour of the product never change",
    "no second copy of the product",
    "no resemblance to any real or famous person",
    "no extra fingers or warped hands",
    "no watermark",
)
# Each capture's format, camera and motion avoid: `ad_template_seed.CAPTURES`.

#: Model-facing words the composer does instead (S19-06).
COMPOSER_WORDS = re.compile(
    r"punch-in|punch in|sped up|speed up|slow-mo|stop-motion|hard cut|jump cut|cuts? to|"
    r"split|side by side|zoom in|zooms in|snap-zoom",
    re.I,
)
_MOVES = re.compile(
    r"(?<!no )\b(dolly|dollies|pan|pans|tilt|tilts|truck|trucks|arc|arcs|orbit|orbits|crane|"
    r"zoom|zooms|whip|push-in|pushes in|pull-out|pulls out|tracks?)\b",
    re.I,
)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_CLAUSE = re.compile(r"[,;] ")


def is_on(workspace: Any) -> bool:
    return flag_enabled(workspace.organization, PRO_RENDERING_S19)


def direction_of(row: CreativeOption) -> dict[str, Any] | None:
    direction = (row.metadata or {}).get("direction")
    return direction if isinstance(direction, dict) else None


def active_direction(row: CreativeOption | None, workspace: Any) -> dict[str, Any] | None:
    """The template's direction when Pro rendering is on for this workspace."""
    if row is None or not is_on(workspace):
        return None
    return direction_of(row)


class RouteCannotError(OCCSError):
    """422: the model this package's route resolves to cannot do what the scene
    needs (S19-09). Refused, never rendered a lesser way."""

    status_code = 422
    default_code = "route_cannot"
    default_detail = "This scene needs something your package's video model cannot do."


NEED_WORDS = {
    "L": "land on the approved still",
    "FL": "open and close on two real photos",
    "AU": "make its own sound",
    "SP": "speak the lines aloud with its own sound",
}
#: A spoken line in a beat (`upgrade_rendering.md` P7): `says: "{hook}"`.
_SAYS = re.compile(r'says: "\{(\w+)\}"')
#: The interface languages a line is spoken in (L-6).
LANGUAGE_NAMES = {"en": "English", "fr": "French", "it": "Italian"}


def speaks(direction: dict[str, Any] | None) -> list[str]:
    """The script parts a scene says aloud, in beat order (S17-38): `hook`,
    `body`, `cta`, or a reason `claim1`…`claim3`. Empty for a silent scene."""
    if not direction:
        return []
    beats = [
        beat for shot in (direction.get("shots") or [direction]) for beat in shot.get("beats") or []
    ]
    return [match for beat in beats for match in _SAYS.findall(str(beat[2]))]


def spoken_windows(direction: dict[str, Any]) -> list[tuple[str, float, float]]:
    """Each spoken part with its beat's share of the clip (start, end in 0..1)."""
    base = float(direction.get("base") or 1)
    out = []
    for beat in direction.get("beats") or []:
        for part in _SAYS.findall(str(beat[2])):
            out.append((part, float(beat[0]) / base, float(beat[1]) / base))
    return out


def capabilities(workspace: Any) -> set[str]:
    """What this workspace's routes can do: land on a frame (L, FL) and make
    sound (AU). Two or more people (MP) is the plan's cast limit
    (`ad_avatars_per_ad`), not a model's, so it is always here."""
    routes = {role: route_profile(workspace, role) for role in ("start", "end")}
    out = {"MP"}
    if routes["end"][1].last_frame:
        out |= {"L", "FL"}
    # Speech asks the model for sound whatever the route's own setting (S17-38).
    if routes["start"][1].audio:
        out.add("SP")
    for role, (route, profile) in routes.items():
        if wants_audio(route, profile):
            out.add(f"AU:{role}")
    return out


def missing(workspace: Any, needs: list[str], can: set[str] | None = None) -> list[str]:
    """The needs the resolved routes cannot meet, in the scene's order. `can`
    is `capabilities(workspace)` when already worked out (the gallery)."""
    can = capabilities(workspace) if can is None else can
    role = "end" if {"L", "FL"} & set(needs) else "start"
    have = {need for need in can if ":" not in need} | ({"AU"} if f"AU:{role}" in can else set())
    return [need for need in needs if need not in have]


#: Slack over a language's speaking rate before a line is too long for its beat.
SPEECH_SLACK = 1.25


class DialogueError(OCCSError):
    """400: a spoken line is missing (`dialogue_required`) or too long for its
    beat (`dialogue_too_long`) — refused before anything is priced or charged."""

    default_code = "dialogue_required"


def words_per_second(language: str) -> float:
    row = CreativeOption.objects.filter(kind=CreativeKind.LANGUAGE, key=language).first()
    return float(((row.metadata or {}) if row else {}).get("wps") or 2.5)


def line_of(part: str, script: dict[str, str]) -> str:
    """A spoken part's words: a script part, or one of the body's reasons."""
    if part.startswith("claim"):
        # By position: an empty middle reason is that reason missing, not the next one.
        reasons = [r.strip() for r in str(script.get("body", "")).split("·")]
        index = int(part.removeprefix("claim") or 1) - 1
        return reasons[index] if 0 <= index < len(reasons) else ""
    return str(script.get(part, "")).strip()


def check_dialogue(
    row: CreativeOption, script: dict[str, str], *, seconds: float, language: str
) -> None:
    """Every line the scene speaks is there and fits its beat (S17-38)."""
    direction = direction_of(row) or {}
    wps = words_per_second(language or "en")
    for part, start, end in spoken_windows(direction):
        line = line_of(part, script)
        if not line:
            raise DialogueError(
                "Write what the person says here before making the video.",
                code="dialogue_required",
                detail={"line": part},
            )
        limit = max(1, math.ceil((end - start) * seconds * wps * SPEECH_SLACK))
        words = len(line.split())
        if words > limit:
            raise DialogueError(
                f"This line is {words} words; its {round((end - start) * seconds)} s fit "
                f"about {limit}.",
                code="dialogue_too_long",
                detail={"line": part, "words": words, "max": limit},
            )


def needs_of(row: CreativeOption, workspace: Any) -> list[str]:
    """What a scene needs from the model: with Pro rendering its own
    requirements (S19-09, unchanged without it), and speech on every package
    (S17-38) — a talking scene sent to a silent model is a broken ad."""
    direction = direction_of(row) or {}
    own = list(direction.get("requires") or []) if is_on(workspace) else []
    return [*own, *(["SP"] if speaks(direction) else [])]


def check_capabilities(workspace: Any, row: CreativeOption) -> None:
    """Refused before any charge when the package's model cannot do what the
    scene needs (`needs_of`)."""
    if direction_of(row) is None:
        return
    lacking = missing(workspace, needs_of(row, workspace))
    if lacking:
        raise RouteCannotError(
            "This scene needs the video model to "
            + " and ".join(NEED_WORDS[need] for need in lacking)
            + ", which your package's model cannot do.",
            detail={"template": row.key, "needs": lacking},
        )


# -----------------------------------------------------------------------------
# Validation (on save, through `ad_templates.validate_metadata`)
# -----------------------------------------------------------------------------
def _timeline(items: list[Any], start: float, end: float, where: str) -> list[str]:
    errors: list[str] = []
    clock = start
    for item in items:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not all(isinstance(n, int | float) for n in item[:2])
            or not isinstance(item[2], str)
            or not item[2].strip()
        ):
            return [f"{where}: each beat is [start, end, action]."]
        if float(item[0]) != clock or float(item[1]) <= float(item[0]):
            errors.append(f"{where}: a gap or an overlap at {clock:g} s.")
        clock = float(item[1])
    if not items or clock != end:
        errors.append(f"{where}: the beats must run from {start:g} to {end:g} s.")
    return errors


def _one_move(camera: str, where: str) -> list[str]:
    moves = {word.lower()[:3] for word in _MOVES.findall(camera)}
    if len(moves) <= 1:
        return []
    return [f"{where}: one camera move per shot ({', '.join(sorted(moves))})."]


def validate_direction(direction: Any) -> list[str]:
    """Beats start at 0, leave no gap or overlap and end at the base length;
    one camera move per shot; no cut inside a shot."""
    if not isinstance(direction, dict):
        return ["direction: an object."]
    errors: list[str] = []
    base = direction.get("base")
    if not isinstance(base, int) or isinstance(base, bool) or not 3 <= base <= 30:
        return ["direction.base: the length the beats are written for, 3 to 30 s."]
    if direction.get("route") not in ROUTES:
        errors.append(f"direction.route: one of {', '.join(ROUTES)}.")
    requires = direction.get("requires", [])
    if not isinstance(requires, list) or set(requires) - set(REQUIREMENTS):
        errors.append(f"direction.requires: a list of {', '.join(REQUIREMENTS)}.")
    errors += _one_move(str(direction.get("camera") or ""), "direction.camera")
    shots = direction.get("shots")
    if shots is not None:
        if not isinstance(shots, list) or not shots:
            return [*errors, "direction.shots: a non-empty list."]
        windows = [[s.get("start"), s.get("end"), "shot"] for s in shots if isinstance(s, dict)]
        if len(windows) != len(shots):
            return [*errors, "direction.shots: each shot is an object."]
        errors += _timeline(windows, 0.0, float(base), "direction.shots")
        for index, shot in enumerate(shots, 1):
            where = f"direction.shots[{index}]"
            if shot.get("use") not in DIRECTION_USES:
                errors.append(f"{where}.use: one of {', '.join(DIRECTION_USES)}.")
            errors += _one_move(str(shot.get("camera") or ""), where)
            if shot.get("beats"):
                errors += _timeline(shot["beats"], float(shot["start"]), float(shot["end"]), where)
    else:
        errors += _timeline(direction.get("beats") or [], 0.0, float(base), "direction.beats")
    return errors


def lint(direction: dict[str, Any]) -> list[str]:
    """Model-facing words that ask for an edit the composer makes (S19-06)."""
    texts = [direction.get(field, "") for field in ("shot", "camera", "subject", "light")]
    texts += [beat[2] for beat in direction.get("beats") or []]
    for shot in direction.get("shots") or []:
        if shot.get("use") in ("frame", "photo", "place"):
            texts.append(shot.get("camera", ""))
            texts += [beat[2] for beat in shot.get("beats") or []]
    return [text for text in texts if text and COMPOSER_WORDS.search(text)]


# -----------------------------------------------------------------------------
# Timing
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Beat:
    start: float
    end: float
    action: str


def _half(value: float) -> float:
    return round(value * 2) / 2


def scaled(beats: list[list[Any]], seconds: float, *, hook: bool = True) -> list[Beat]:
    """The beats moved onto a clock of `seconds`, from 0.

    With `hook`, the first beat keeps its own end (at most half the clip), so
    a 5, 10 or 15 s render opens on the same 0-2 s; the rest stretch or shrink
    in proportion. Boundaries land on half seconds and stay increasing.
    """
    if not beats:
        return []
    origin = float(beats[0][0])
    span = float(beats[-1][1]) - origin
    first = float(beats[0][1]) - origin
    first = min(first, seconds / 2) if hook else first * seconds / span
    rest_from, rest_to = float(beats[0][1]) - origin, span
    edges = [0.0, first]
    for beat in beats[1:]:
        share = (float(beat[1]) - origin - rest_from) / (rest_to - rest_from or 1)
        edges.append(first + share * (seconds - first))
    edges = [_half(edge) for edge in edges]
    edges[-1] = seconds
    for index in range(1, len(edges)):
        edges[index] = max(edges[index], edges[index - 1] + 0.5)
    edges[-1] = max(edges[-1], seconds)
    return [
        Beat(edges[index], edges[index + 1], str(beat[2]).strip())
        for index, beat in enumerate(beats)
    ]


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def timeline(beats: list[Beat], syntax: str) -> str:
    """The beats in the profile's syntax (§6.2)."""
    if syntax == "none":
        return " Then ".join(beat.action[:1].upper() + beat.action[1:] for beat in beats)
    if syntax == "brackets":
        parts = [f"[{_clock(b.start)}-{_clock(b.end)}] {b.action}" for b in beats]
    elif syntax == "shots":
        parts = [f"Shot {i} ({b.start:g}-{b.end:g} s): {b.action}" for i, b in enumerate(beats, 1)]
    else:
        parts = [f"{b.start:g}-{b.end:g} s: {b.action}" for b in beats]
    return " ".join(part if part.endswith(".") else f"{part}." for part in parts)


# -----------------------------------------------------------------------------
# Compile
# -----------------------------------------------------------------------------
#: What an over-budget prompt loses first. The scene and its light are in the
#: frame the model is given and told to keep; the sound is not (S17-43).
TRIM_ORDER = ("style", "scene", "light", "sound")
ORDER = (
    "format",
    "camera",
    "subject",
    "action",
    "keep",
    "note",
    "scene",
    "light",
    "style",
    "sound",
    "avoid",
)
LEADS = {
    "camera": "Camera: ",
    "note": "Direction: ",
    "scene": "Setting: ",
    "light": "Light: ",
    "style": "Style: ",
    "sound": "Sound: ",
    "avoid": "Avoid: ",
}


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".;, ")
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def _words(parts: dict[str, str]) -> int:
    return sum(len(text.split()) for text in parts.values())


def compile_parts(parts: dict[str, str], budget: int) -> str:
    """The parts in their order, each led by its label, trimmed to `budget`."""
    parts = {key: _sentence(parts.get(key, "")) for key in ORDER}
    for key in TRIM_ORDER:
        while parts[key] and _words({k: LEADS.get(k, "") + v for k, v in parts.items()}) > budget:
            clauses = _CLAUSE.split(parts[key].rstrip("."))
            parts[key] = _sentence(", ".join(clauses[:-1])) if len(clauses) > 1 else ""
    return " ".join(LEADS.get(key, "") + parts[key] for key in ORDER if parts[key])


def fill(text: str, values: dict[str, str]) -> str:
    """Placeholders filled; one with no value reads as plain words, never braces."""
    return _PLACEHOLDER.sub(
        lambda match: values.get(match.group(1)) or match.group(1).replace("_", " "), text
    )


@dataclass(frozen=True)
class Shot:
    """One model call's share of a scene: its beats, camera and window."""

    use: str
    start: float
    end: float
    camera: str
    beats: list[Beat]


def shots_for(direction: dict[str, Any], seconds: float) -> list[Shot]:
    """The scene's shots on the render's clock (one for a single clip); the end
    card is the composer's and is left out."""
    base = float(direction["base"])
    if not direction.get("shots"):
        return [
            Shot(
                use="frame",
                start=0.0,
                end=seconds,
                camera=str(direction.get("camera") or ""),
                beats=scaled(direction.get("beats") or [], seconds),
            )
        ]
    out: list[Shot] = []
    for shot in direction["shots"]:
        if shot.get("use") == "end":
            continue
        start = float(shot["start"]) * seconds / base
        end = float(shot["end"]) * seconds / base
        out.append(
            Shot(
                use=str(shot["use"]),
                start=start,
                end=end,
                camera=str(shot.get("camera") or ""),
                # Each shot is its own call, on its own clock from 0.
                beats=scaled(shot.get("beats") or [], end - start, hook=not out),
            )
        )
    return out


def on_clock(shot: Shot, seconds: float, *, hook: bool) -> Shot:
    """The shot as one model call: its beats moved onto `seconds` from 0, the
    length the model is asked for."""
    beats = [[beat.start, beat.end, beat.action] for beat in shot.beats]
    return Shot(shot.use, 0.0, seconds, shot.camera, scaled(beats, seconds, hook=hook))


def _light(row: CreativeOption | None) -> str:
    """A light chip, its keyword first (§8)."""
    if row is None:
        return ""
    keyword = str((row.metadata or {}).get("keyword") or "").strip()
    fragment = row.prompt_fragment.strip()
    return f"{keyword}, {fragment}" if keyword and keyword not in fragment else fragment


@dataclass(frozen=True)
class Context:
    """What a render brings to its prompt, read once per render."""

    direction: dict[str, Any]
    capture: str
    values: dict[str, str]
    subject: str
    note: str
    scene: str
    light: str
    style: str
    ambient: str
    avoid: str
    camera_hint: str
    #: The language the lines are spoken in (S17-38).
    language: str = "en"


def context_for(render: Any, template: CreativeOption) -> Context:
    from ai.services import ad_templates, storyboard
    from ai.services.creative import _chosen, _moods
    from brand.services import avatars
    from products.services import identity

    # S20: the render's own storyboard, when the person edited the scenes.
    direction = storyboard.direction_for(render, template)
    assert direction is not None
    meta = template.metadata
    spec = render.spec
    creative = dict(spec.get("creative") or {})
    product = render.product
    script = dict(spec.get("script") or {})
    capture = str(direction.get("capture") or meta.get("capture") or "phone_ugc")
    try:
        place = ad_templates.place_of(
            template, str(spec.get("location") or ""), workspace=render.workspace
        )
    except (ad_templates.TemplateRefusedError, NotFound):
        # A place retired after the render was paid for: no words, never a failure.
        place = ad_templates.Place(set="")
    # The frame shows the product and the person: the beats call them so, and
    # SUBJECT names them once.
    values = {
        "product": "the product",
        "person": "the person",
        "person_a": "the first person",
        "person_b": "the second person",
        "problem": (product.top_objection or product.buyer_situation) if product else "",
        # A spoken line sits inside quotes: its own double quotes become single.
        **{key: str(value).replace('"', "'") for key, value in script.items()},
        **_scene_values(product, script, spec),
    }
    people = []
    if render.avatar is not None:
        people.append(avatars.describe(render.avatar))
    vibe = _chosen(creative, CreativeKind.VIBE)
    performance = str(((vibe.metadata or {}) if vibe else {}).get("performance") or "")
    subject = "; ".join(
        part
        for part in [
            f"The product is {product.name}" if product else "",
            fill(str(direction.get("subject") or ""), values),
            *identity.lines(product),
            *people,
            f"performance: {performance}" if people and performance else "",
            fill(str(direction.get("attitude") or ""), values) if people else "",
        ]
        if part
    )
    mood_rows = _moods(creative)
    # A mood's own camera move never applies: the template's shot is the camera
    # (S17-43). Its palette is in the first frame, which the motion keeps.
    light = (
        fill(str(direction["light"]), values)
        if direction.get("light")
        else _light(_chosen(creative, CreativeKind.LIGHT)) or place.light
    )
    words = CAPTURES.get(capture, {})
    avoid = "; ".join(
        part
        for part in [
            *GLOBAL_AVOID,
            words.get("video_avoid", ""),
            fill(str(direction.get("avoid") or ""), values).rstrip("."),
            place.forbid,
            str(creative.get("avoid") or "").strip(),
        ]
        if part
    )
    return Context(
        language=str(spec.get("language") or "en"),
        direction=direction,
        capture=capture,
        values=values,
        subject=subject,
        note=str(spec.get("prompt") or ""),
        scene=", ".join(part for part in [place.set, place.zone, place.depth] if part),
        light=light,
        style="; ".join(
            part.rstrip(".")
            for part in [
                words.get("finish", ""),
                *(
                    str((row.metadata or {}).get("visible") or row.prompt_fragment)
                    for row in mood_rows
                ),
            ]
            if part
        ),
        ambient=place.ambient,
        avoid=avoid,
        camera_hint=words.get("video_camera", ""),
    )


def _scene_values(product: Any, script: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    """The new scenes' placeholders (S19-12), filled only from what the brand
    gave: its card's parts, its script's benefits, its proven claims, its
    offer. Anything missing reads as plain words, never as an invention."""
    from ai.services.ad_templates import card_parts, proven_claims

    if product is None:
        return {}
    parts = card_parts(product)
    benefits = [
        part.strip().replace('"', "'")
        for part in str(script.get("body", "")).split("·")
        if part.strip()
    ]
    claims = proven_claims(product)
    offer = dict(spec.get("offer") or {})
    others = list(spec.get("extra_products") or [])
    return {
        **{f"ingredient_{i + 1}": part for i, part in enumerate(parts[:3])},
        **({"ingredients": ", ".join(parts), "parts": ", ".join(parts)} if parts else {}),
        **{f"step_{i + 1}": step for i, step in enumerate(benefits[:3])},
        **{f"claim{i + 1}": claim for i, claim in enumerate(benefits[:3])},
        **({"test": claims[0].replace("-", " ")} if claims else {}),
        "material": "fine powder" if product.offer_kind == "beauty" else "water droplets",
        "task": product.buyer_situation or "the work",
        "use": "uses it",
        "action": "uses it",
        "offer": str(offer.get("text") or ""),
        "launch_date": str(offer.get("ends") or ""),
        "variant_a": "the first colour",
        "variant_b": "the second colour",
        **{f"product_{i + 2}": "the next product" for i in range(len(others[:2]))},
        "product_1": "the product",
    }


#: What the model is told about the image it is given (goose-skills `video-h3max`'s
#: light lock; the Omni guide's "do not change facial structure or clothing").
KEEP_FRAME = {
    "first": "Image 1 is the first frame: keep its place, people, clothes, product and light",
    "last": "Image 1 is the last frame: end on it, same place, people, product and light",
}


def prompt_for(
    context: Context,
    shot: Shot,
    profile: VideoModelProfile,
    *,
    audio: bool,
    frame: str = "first",
) -> str:
    """One model call's prompt: the scene's parts around this shot's beats, and
    what to keep of the image it starts (or ends) on."""
    direction = context.direction
    values = context.values
    framing = fill(str(direction.get("shot") or ""), values).rstrip(".")
    format_line = ", ".join(
        part
        for part in [
            "Vertical 9:16",
            framing,
            CAPTURES.get(context.capture, {}).get("video_format", ""),
        ]
        if part
    )
    sound = ""
    keep = KEEP_FRAME.get(frame, "")
    if speaks(direction):
        # The person's own voice and nothing else — music is the composer's bed —
        # kept with the frame, which no word budget trims (S17-38).
        language = LANGUAGE_NAMES.get(context.language, "English")
        keep = "; ".join(
            part
            for part in [
                keep,
                f"sound: only the person's own voice speaking the quoted lines in {language}, "
                "lips moving with the words, no music",
            ]
            if part
        )
    elif audio:
        sound = "; ".join(
            part
            for part in [
                context.ambient,
                fill(str(direction.get("sound") or ""), values).rstrip("."),
                "no music",
                "no speech",
            ]
            if part
        )
    parts = {
        "format": format_line,
        "camera": fill(shot.camera, values) or context.camera_hint,
        "subject": context.subject,
        "action": timeline(
            [Beat(b.start, b.end, fill(b.action, values)) for b in shot.beats], profile.timecodes
        ),
        "keep": keep,
        "note": context.note,
        "scene": context.scene,
        "light": context.light,
        "style": context.style,
        "sound": sound,
        "avoid": context.avoid,
    }
    return compile_parts(parts, profile.budget_words)


def beat_marks(
    direction: dict[str, Any], seconds: float, *, card: float = 0.0
) -> list[tuple[float, str]]:
    """The middle of each beat on the delivered clip's clock, with its action
    (S19-13): what a reviewer checks the render against. A reel's shots sit
    before its end card, each beat offset by its shot's start."""
    body = seconds - card if direction.get("shots") else seconds
    marks: list[tuple[float, str]] = []
    for shot in shots_for(direction, body):
        for beat in shot.beats:
            middle = round(shot.start + (beat.start + beat.end) / 2, 2)
            marks.append(
                (middle, fill(beat.action, {"product": "the product", "person": "the person"}))
            )
    return marks


def route_profile(workspace: Any, role: str) -> tuple[Any, VideoModelProfile]:
    """The route a call will take and its model's profile. The fallback model
    gets the same words: compiled for the primary (shortcut: compile per model
    if a fallback's renders drift)."""
    from ai.services.routing import resolve_for

    route = resolve_for(AITask.VIDEO_END if role == "end" else AITask.VIDEO_START, workspace)
    return route, VideoModelProfile.for_slug(route.model)


def wants_audio(route: Any, profile: VideoModelProfile) -> bool:
    """Native sound: the route asks for it and the model makes it."""
    return bool(route.params.get("audio")) and profile.audio
