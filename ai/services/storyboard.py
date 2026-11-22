"""S20 — a template's scenes, edited (`studio_storyboard.md`).

A single-shot template's timed beats are its scenes. The person may reorder,
retime, drop and reword them, or add a ready-made `story_scene` row or a scene
in their own words. The list becomes the beats of the one directed render, on
the render's own clock, at the price the template already has (D1).

Everything is checked here, at the estimate, so a storyboard that cannot be
rendered as asked is a 400 before any charge — never a failed render later.
A multi-shot template's scenes are fixed shots and are not edited here.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ai.models import CreativeKind, CreativeOption
from billing.services.flags import STUDIO_STORYBOARD_S20, flag_enabled
from common.exceptions import OCCSError

MAX_SCENES = 6
MIN_SECONDS = 1.0
#: The person's own words for one scene; the same cap as a template direction.
WORDS_MAX = 240
_BEAT = re.compile(r"^beat:(\d+)$")
_SAYS = re.compile(r'says: "\{(\w+)\}"')
_PERSON_LINE = "{person} is in the shot."


class StoryboardOffError(OCCSError):
    """400: a storyboard was sent while the step is off for this organization."""

    default_code = "storyboard_off"
    default_detail = "Scene-by-scene editing is not available yet."


class StoryboardInvalidError(OCCSError):
    """400: the storyboard cannot be rendered as asked. `detail.field` names
    what to fix: `count`, `seconds`, `scene`, `words`, `person`, `speech` or
    `template`."""

    default_code = "storyboard_invalid"
    default_detail = "This storyboard cannot be made as it is."


def is_on(workspace: Any) -> bool:
    return flag_enabled(workspace.organization, STUDIO_STORYBOARD_S20)


def editable(row: CreativeOption) -> bool:
    """A one-clip template with timed beats; a multi-shot one is fixed shots."""
    from ai.services import ad_templates, video_prompt

    direction = video_prompt.direction_of(row) or {}
    return bool(direction.get("beats")) and not (
        direction.get("shots") or ad_templates.shots_of(row)
    )


def _fill(text: str, product_name: str) -> str:
    from ai.services import video_prompt

    return video_prompt.fill(text, {"product": product_name, "person": "the person"})


def scenes_of(row: CreativeOption, product_name: str = "the product") -> list[dict[str, Any]]:
    """The template's own scenes for the Studio: label, action in plain words,
    its share of the clip (0..1) and whether it is spoken. `[]` when fixed."""
    from ai.services import ad_templates, video_prompt

    if not editable(row):
        return []
    direction = video_prompt.direction_of(row) or {}
    beats = direction["beats"]
    base = float(direction.get("base") or beats[-1][1])
    labels = [
        beat for beat in row.metadata.get("beats") or [] if beat not in ad_templates.CARD_BEATS
    ]
    out = []
    for index, (start, end, action) in enumerate(beats):
        plain = _SAYS.sub("speaks", str(action))
        out.append(
            {
                # The template's own scene names first; a beat past them is named by its action.
                "label": labels[index] if index < len(labels) else _short(plain),
                "action": _fill(plain, product_name).strip(),
                "start": round(float(start) / base, 4),
                "end": round(float(end) / base, 4),
                "speaks": bool(_SAYS.search(str(action))),
            }
        )
    return out


def _short(action: str) -> str:
    words = action.strip().rstrip(".").split()
    text = " ".join(words[:5])
    return (text[:1].upper() + text[1:]) if text else ""


def _refuse(field: str, message: str, **detail: Any) -> StoryboardInvalidError:
    return StoryboardInvalidError(message, detail={"field": field, **detail})


def _clean_words(workspace: Any, raw: Any) -> str:
    from ai.services import ad_templates, prompt_lint, video_prompt

    text = " ".join(str(raw or "").split())[:WORDS_MAX]
    if not text:
        return ""
    # The plan's `ad_prompt_edit`: 402 without it, as for a template direction.
    ad_templates.clean_prompt(workspace, text)
    if video_prompt.COMPOSER_WORDS.search(text) or prompt_lint.problems(text, person=False):
        raise _refuse(
            "words",
            "Describe what the camera sees: edits, captions and hashtags are added "
            "afterwards, not by the video model.",
        )
    return text


def parse(
    workspace: Any,
    row: CreativeOption,
    raw: Any,
    *,
    seconds: int,
    person_on: bool,
) -> list[dict[str, Any]]:
    """The storyboard as stored on the render: `[{scene, seconds, words, person}]`.
    Raises `StoryboardOffError` or `StoryboardInvalidError`."""
    from ai.services import video_prompt

    if not is_on(workspace):
        raise StoryboardOffError
    if not editable(row):
        raise _refuse("template", "This template's scenes are fixed shots; pick them as they are.")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SCENES:
        raise _refuse("count", f"A storyboard has 1 to {MAX_SCENES} scenes.", max=MAX_SCENES)
    own = (video_prompt.direction_of(row) or {})["beats"]
    rows = {
        option.key: option
        for option in CreativeOption.objects.filter(kind=CreativeKind.STORY_SCENE, is_active=True)
    }
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _refuse("scene", "Each scene is an object.")
        key = str(item.get("scene") or "")
        try:
            length = float(item.get("seconds"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            length = 0.0
        if not math.isfinite(length) or length < MIN_SECONDS:
            raise _refuse(
                "seconds", f"Each scene lasts at least {MIN_SECONDS:g} s.", min=MIN_SECONDS
            )
        words = _clean_words(workspace, item.get("words"))
        person = bool(item.get("person"))
        beat = _BEAT.match(key)
        if beat:
            if int(beat.group(1)) >= len(own):
                raise _refuse("scene", "That scene is not in this template.", scene=key)
        elif key == "custom":
            if not words:
                raise _refuse("words", "Say what happens in your own scene.")
        elif key in rows:
            needs = str((rows[key].metadata or {}).get("person") or "none")
            if needs == "required" and not person_on:
                raise _refuse("person", "This scene shows a person: add one to the ad.", scene=key)
            if needs == "none":
                person = False
        else:
            raise _refuse("scene", "That scene is not in the catalogue.", scene=key)
        if person and not person_on:
            raise _refuse("person", "Add a person to the ad to show one in this scene.")
        out.append({"scene": key, "seconds": length, "words": words, "person": person})

    total = sum(scene["seconds"] for scene in out)
    if abs(total - seconds) > 0.01:
        raise _refuse(
            "seconds",
            f"The scenes add up to {total:g} s; this length is {seconds} s.",
            total=total,
            expected=seconds,
        )
    spoken = {index for index, beat in enumerate(own) if _SAYS.search(str(beat[2]))}
    kept = {int(m.group(1)) for s in out if (m := _BEAT.match(s["scene"]))}
    if spoken - kept:
        raise _refuse(
            "speech", "Keep every scene where the person speaks.", missing=sorted(spoken - kept)
        )
    direction = with_beats(row, out)
    problems = video_prompt.validate_direction(direction) + video_prompt.lint(direction)
    if problems:
        raise _refuse("words", "This storyboard cannot be filmed as written.", problems=problems)
    return out


def _action(row: CreativeOption, scene: dict[str, Any], rows: dict[str, CreativeOption]) -> str:
    from ai.services import video_prompt

    key = scene["scene"]
    words = scene.get("words") or ""
    beat = _BEAT.match(key)
    if beat:
        base = str((video_prompt.direction_of(row) or {})["beats"][int(beat.group(1))][2])
        if words:
            # The person's words replace the action; a spoken line is kept.
            says = " ".join(f'says: "{{{part}}}".' for part in _SAYS.findall(base))
            base = " ".join(filter(None, [words.rstrip(".") + ".", says]))
    elif key == "custom":
        base = words.rstrip(".") + "."
    else:
        base = rows[key].prompt_fragment
        if words:
            base = f"{base.rstrip('.')}, {words.rstrip('.')}."
    if scene.get("person"):
        base = f"{base.rstrip()} {_PERSON_LINE}"
    return base.strip()


def with_beats(row: CreativeOption, storyboard: list[dict[str, Any]]) -> dict[str, Any]:
    """The template's direction with the storyboard as its beats, on a clock of
    the storyboard's own length. Everything else — shot, camera, light, sound,
    avoid — stays the template's."""
    from ai.services import video_prompt

    direction = dict(video_prompt.direction_of(row) or {})
    rows = {
        option.key: option
        for option in CreativeOption.objects.filter(
            kind=CreativeKind.STORY_SCENE,
            key__in=[scene["scene"] for scene in storyboard],
        )
    }
    beats: list[list[Any]] = []
    clock = 0.0
    for scene in storyboard:
        end = clock + float(scene["seconds"])
        beats.append([clock, end, _action(row, scene, rows)])
        clock = end
    total = round(clock)
    if beats:
        beats[-1][1] = total
    direction["beats"] = beats
    direction["base"] = int(total)
    return direction


def direction_for(render: Any, row: CreativeOption | None) -> dict[str, Any] | None:
    """The direction a render is made from: the storyboard's when it has one,
    else the template's own."""
    from ai.services import video_prompt

    if row is None:
        return None
    storyboard = (render.spec or {}).get("storyboard")
    if storyboard:
        return with_beats(row, storyboard)
    return video_prompt.direction_of(row)
