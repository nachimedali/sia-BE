"""Which Studio controls a template takes (S17-43, `studio_controls_1911.md`).

A template owns its place, its camera and who is in frame; what else it lets
the person change follows from its own data — its places, its capture, its
light and attitude, the calm or not of its action — and the option rows (each
light's `conflicts`). An admin may override any control in the template's
`metadata["controls"]`. The Studio disables what does not fit and the server
refuses it with a 400 `not_in_template`, from this one module, so the two can
never disagree.

Why moods are narrowed: every mood row carries a camera move and most a set
("a seamless backdrop", "the product on the left third"); on a template the
camera is the template's and the set is its place, so only grade-like moods
stay. Beta render #7 is what an unchecked look does: `cast: product-only` on a
person template, and the man vanished.
"""

from __future__ import annotations

import re
from typing import Any

from ai.models import CreativeKind, CreativeOption
from ai.services.ad_templates import TemplateRefusedError

LOOK_KINDS = ("scene", "camera", "cast", "light", "palette", "moods", "vibe")
#: Free words, never a choice to refuse: kept and sent to both sides.
FREE_KINDS = ("avoid",)
BRAND_SET = "brand-colour"
BRAND_PALETTE = "brand"
#: Why a control or an option is off — the Studio names the reason by this code.
REASONS = (
    "template_place",
    "template_shot",
    "avatar_step",
    "template_light",
    "capture_light",
    "place_light",
    "grade_only",
    "screen_look",
    "template_attitude",
    "calm_action",
    "two_people",
    "no_person",
    "brand_set",
    "place_setting",
    "place_untagged",
)
_ALWAYS_LOCKED = {"scene": "template_place", "camera": "template_shot", "cast": "avatar_step"}
_CALM = re.compile(r"\b(lying|lies|relax\w*|calm\w*|slow\w*|sleep\w*|still)\b", re.I)


class NotInTemplateError(TemplateRefusedError):
    """400: a choice this template does not take — which control, the value,
    what it does take and why, so the page can say exactly what to change."""

    default_code = "not_in_template"

    def __init__(self, control: str, value: Any, allowed: Any, reason: str) -> None:
        super().__init__(
            f"'{value}' does not fit this template's {control}.",
            code="not_in_template",
            detail={"control": control, "value": value, "allowed": allowed, "reason": reason},
        )


def _keys(kind: str) -> list[str]:
    return list(
        CreativeOption.objects.filter(kind=kind, is_active=True)
        .order_by("sort_order", "key")
        .values_list("key", flat=True)
    )


def _place_rows() -> dict[str, dict[str, Any]]:
    return {
        row.key: dict(row.metadata or {})
        for row in CreativeOption.objects.filter(kind=CreativeKind.PLACE, is_active=True)
    }


def setting_of(place_key: str) -> str:
    """The kind of place a catalogue place is (kitchen, beach, …); "" unknown."""
    return str(_place_rows().get(place_key, {}).get("setting") or "")


def light_conflicts(light_key: str) -> list[str]:
    row = CreativeOption.objects.filter(kind=CreativeKind.LIGHT, key=light_key).first()
    return list(((row.metadata or {}) if row else {}).get("conflicts") or [])


def _derive(row: CreativeOption) -> dict[str, dict[str, Any]]:
    meta = row.metadata or {}
    direction = meta.get("direction") or {}
    capture = str(meta.get("capture") or "phone_ugc")
    person = str(meta.get("person") or "none")
    places = list(meta.get("locations") or [])
    rows = _place_rows()
    outdoor = any(rows.get(place, {}).get("outdoor") for place in places)
    action = " ".join([str(meta.get("motion_prompt") or ""), *map(str, meta.get("beats") or [])])
    two = "person_b" in str(meta) or bool(direction.get("side_by_side"))
    studio = capture in ("studio_hero", "macro_tabletop")
    out: dict[str, dict[str, Any]] = {
        kind: {"locked": True, "reason": reason} for kind, reason in _ALWAYS_LOCKED.items()
    }

    if direction.get("light"):
        out["light"] = {"locked": True, "reason": "template_light"}
    elif capture != "studio_hero":
        out["light"] = {"locked": True, "reason": "capture_light"}
    else:
        conflicts = {key: set(light_conflicts(key)) for key in _keys(CreativeKind.LIGHT)}
        out["light"] = {
            "open": [key for key, bad in conflicts.items() if any(p not in bad for p in places)],
            "reason": "place_light",
        }

    moods_live = set(_keys(CreativeKind.MOOD))
    if capture == "phone_ugc":
        moods = ["film", *([] if direction.get("light") else ["natural"])]
    elif studio:
        moods = ["cinematic", "film"]
        if not direction.get("light"):
            moods += ["vibrant", *([] if outdoor else ["warm"])]
    else:
        moods = []
    out["moods"] = (
        {"open": [m for m in moods if m in moods_live], "reason": "grade_only"}
        if moods
        else {"locked": True, "reason": "screen_look"}
    )

    product_vibes = ["everyday", "elevated-minimal", *(["luxe"] if studio else [])]
    if person == "none":
        out["vibe"] = {"open": product_vibes, "product_only": product_vibes, "reason": "no_person"}
    elif direction.get("attitude"):
        out["vibe"] = {"locked": True, "reason": "template_attitude"}
    else:
        calm = bool(_CALM.search(action))
        vibes = ["everyday", "luxe", "elevated-minimal", *([] if calm else ["street", "active"])]
        if two:
            vibes.append("family")
        out["vibe"] = {
            "open": vibes,
            "product_only": ["everyday", "elevated-minimal"],
            "reason": "calm_action" if calm else ("two_people" if not two else "no_person"),
        }
    vibes_live = set(_keys(CreativeKind.VIBE))
    if "open" in out["vibe"]:
        out["vibe"]["open"] = [v for v in out["vibe"]["open"] if v in vibes_live]
        out["vibe"]["product_only"] = [v for v in out["vibe"]["product_only"] if v in vibes_live]

    out["palette"] = (
        {"locked": True, "reason": "brand_set"}
        if places == [BRAND_SET]
        else {"open": "any", "reason": "brand_set"}
    )
    return out


def controls(row: CreativeOption) -> dict[str, Any]:
    """Every look control of this template — `{"locked": True, "reason"}` or
    `{"open": [keys] | "any", "reason"}` — then the admin's override per control,
    and the places with the settings an own place must have."""
    derived = _derive(row)
    override = (row.metadata or {}).get("controls") or {}
    out: dict[str, Any] = {kind: {**derived[kind], **override.get(kind, {})} for kind in derived}
    for kind, value in out.items():
        if value.get("locked"):
            out[kind] = {"locked": True, "reason": value.get("reason", "template_place")}
    places = list((row.metadata or {}).get("locations") or [])
    out["places"] = {
        "keys": places,
        "settings": sorted({s for s in (setting_of(p) for p in places) if s}),
    }
    return out


def served(row: CreativeOption, table: dict[str, Any] | None = None) -> dict[str, Any]:
    """What `GET /ai/ad-templates/` sends with each template: every look
    control in one typed shape — locked, or its open keys (`any` for all) —
    with the reason the page shows on what is off."""
    table = table or controls(row)
    out: dict[str, Any] = {}
    for kind in LOOK_KINDS:
        rule = table[kind]
        opened = rule.get("open")
        out[kind] = {
            "locked": bool(rule.get("locked")),
            "any": opened == "any",
            "open": list(opened) if isinstance(opened, list) else [],
            "product_only": list(rule.get("product_only") or []),
            "reason": str(rule.get("reason") or ""),
        }
    out["places"] = table["places"]
    return out


def own_place_fits(row: CreativeOption, place: Any, table: dict[str, Any] | None = None) -> bool:
    table = table or controls(row)
    return bool(place.stands_for) and setting_of(place.stands_for) in table["places"]["settings"]


def _place_key(row: CreativeOption, location: str, workspace: Any, table: dict[str, Any]) -> str:
    """The catalogue place the chosen place is: itself, or what an own place
    stands for. Another brand's own place is a 404, before any fit."""
    from brand.services import places

    chosen = location or (table["places"]["keys"] or [""])[0]
    if not chosen.startswith(places.OWN):
        return chosen
    place = places.own(workspace, chosen)
    if not place.stands_for:
        raise NotInTemplateError("place", chosen, table["places"]["settings"], "place_untagged")
    if not own_place_fits(row, place, table):
        raise NotInTemplateError("place", chosen, table["places"]["settings"], "place_setting")
    return str(place.stands_for)


def _refused(
    kind: str, value: Any, rule: dict[str, Any], *, place: str, person_on: bool
) -> tuple[Any, str] | None:
    """(allowed, reason) when `value` does not fit `rule`; None when it does."""
    if rule.get("locked"):
        return [], rule["reason"]
    allowed = rule.get("open")
    if kind == "vibe" and not person_on:
        allowed = rule.get("product_only", allowed)
        if value not in (allowed or []):
            return allowed, "no_person"
    if allowed != "any" and value not in (allowed or []):
        return allowed, rule.get("reason", "")
    if kind == "light" and place in light_conflicts(str(value)):
        return allowed, "place_light"
    if kind == "palette" and place == BRAND_SET and value != BRAND_PALETTE:
        return [BRAND_PALETTE], "brand_set"
    return None


def check(
    row: CreativeOption,
    creative: dict[str, Any] | None,
    *,
    location: str,
    person_on: bool,
    workspace: Any = None,
) -> dict[str, Any]:
    """The look this template takes, as given — or a 400 `not_in_template`
    naming the first choice that does not fit. Never silently trimmed: a
    choice the person made is either honoured on both sides or refused."""
    table = controls(row)
    place = _place_key(row, location, workspace, table)
    look = dict(creative or {})
    for kind in LOOK_KINDS:
        value = look.get(kind)
        if not value:
            continue
        for one in value if isinstance(value, list) else [value]:
            refused = _refused(kind, one, table[kind], place=place, person_on=person_on)
            if refused is not None:
                raise NotInTemplateError(kind, one, *refused)
    return look


def allowed_part(
    row: CreativeOption,
    creative: dict[str, Any] | None,
    *,
    location: str,
    person_on: bool,
    workspace: Any = None,
) -> dict[str, Any]:
    """The part of a Studio look this template takes, the rest dropped — what
    the Studio sends from its state (the frontend mirrors this; the tests hold
    both to the same answer)."""
    table = controls(row)
    place = _place_key(row, location, workspace, table)
    out: dict[str, Any] = {}
    for kind, value in dict(creative or {}).items():
        if kind in FREE_KINDS:
            if value:
                out[kind] = value
            continue
        if kind not in LOOK_KINDS or not value:
            continue
        keep = [
            one
            for one in (value if isinstance(value, list) else [value])
            if _refused(kind, one, table[kind], place=place, person_on=person_on) is None
        ]
        if keep:
            out[kind] = keep if isinstance(value, list) else keep[0]
    return out


def validate(raw: Any) -> list[str]:
    """Errors in an admin's `metadata["controls"]`, for `validate_metadata`."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["controls: expected an object."]
    errors = []
    for kind, value in raw.items():
        if kind not in LOOK_KINDS:
            errors.append(f"controls.{kind}: one of {', '.join(LOOK_KINDS)}.")
            continue
        if not isinstance(value, dict):
            errors.append(f"controls.{kind}: expected an object.")
        elif value.get("locked") is True:
            continue
        elif not (
            value.get("open") == "any"
            or (
                isinstance(value.get("open"), list)
                and all(isinstance(v, str) for v in value["open"])
            )
        ):
            errors.append(f'controls.{kind}: {{"locked": true}} or {{"open": [keys] | "any"}}.')
    return errors
