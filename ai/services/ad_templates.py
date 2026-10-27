"""Studio video ad templates (S17, `ads_templates.md`, `avatar_upgrade/analysis.md`).

```
gallery     product → every template, each with whether it fits and whether the
            plan unlocks it (locked tiles are shown, never hidden)
resolve     template key + product + avatar → the row, or the reason it cannot
            be made: 404 unknown · 400 does not fit · 402 locked · 409 not yet
keyframe    the first frame, an IMAGE generation on the existing pipeline,
            paid in credits on pass; a re-roll counts against `ad_rerolls`
prompts     the keyframe and motion prompts, filled from the row
```

The video itself goes through `ai.services.video` like any clip: a template
render is a clip whose motion comes from the template row and whose price is
the row's `units` for the chosen length.

**The plan decides, the server enforces.** Every lock the gallery shows is
raised again here at the point of use; the page only mirrors it. Talking
formats are listed and locked as "not yet" (`available: false`) until speech
ships (T4) — a tile nobody can render is a preview of what is coming, never a
button that fails.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import NotFound, ValidationError

from ai.models import CreativeKind, CreativeOption, Generation, GenerationKind, GenerationMode
from ai.services import references
from billing.services.entitlements import Entitlements, entitlements_for
from billing.services.flags import AD_TEMPLATES_S17, flag_enabled
from billing.services.plans import suggest_upgrade
from common.exceptions import FeatureNotAvailable, OCCSError, StateConflict
from products.models import Product
from workspaces.models import Workspace

CATEGORIES = ("unbox", "hand", "tryon", "app", "tour", "talk", "interview", "fx")
OFFER_KINDS = ("physical", "beauty", "app", "service", "space")
PERSON = ("required", "optional", "none")
#: Which plan feature unlocks each tier.
TIER_FEATURE = {
    "product": "ad_templates",
    "people": "ad_templates_people",
    "talking": "ad_templates_talking",
}
NEEDS = ("", "app_screenshot")
#: A multi-shot template's shots (T3): `frame` animates the first frame,
#: `photo` animates the product's next photo, `screen` sets its app screenshot
#: in as a still — never through the video model, which would blur it.
SHOT_USES = ("frame", "photo", "screen")
SHOTS_MIN, SHOTS_MAX = 2, 5
#: The shot tag that marks a product photo as an app screenshot.
SCREEN_TAG = "screen"
PROMPT_MAX = 900
SCRIPT_MAX = 400
KEYFRAME_ASPECT = "9:16"


class TemplateRefusedError(OCCSError):
    """400 with its own code — `template_fit`, `needs_screenshot`,
    `person_required`, `no_person`, `template_length`, `template_location`,
    `no_frame` — so
    the page can say exactly what to change."""

    default_code = "template_refused"
    default_detail = "This template cannot be made this way."


class TemplateUnavailableError(StateConflict):
    default_code = "template_unavailable"
    default_detail = "This template is not available yet."


class RerollLimitError(FeatureNotAvailable):
    default_code = "reroll_limit_reached"
    default_detail = "This ad has used all its re-rolls."


# -----------------------------------------------------------------------------
# The row
# -----------------------------------------------------------------------------
def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def validate_metadata(meta: Any) -> None:
    """What `CreativeOption.clean` holds an `ad_template` row to, so an admin
    edit cannot save a template the service would then fail on."""
    errors: dict[str, str] = {}
    if not isinstance(meta, dict):
        raise DjangoValidationError({"metadata": "Expected an object."})
    if meta.get("category") not in CATEGORIES:
        errors["category"] = f"One of {', '.join(CATEGORIES)}."
    fits = meta.get("fits")
    if not _strings(fits) or not fits or set(fits) - set(OFFER_KINDS):
        errors["fits"] = f"A non-empty list of {', '.join(OFFER_KINDS)}."
    if meta.get("person") not in PERSON:
        errors["person"] = f"One of {', '.join(PERSON)}."
    if meta.get("tier") not in TIER_FEATURE:
        errors["tier"] = f"One of {', '.join(TIER_FEATURE)}."
    lengths, units = meta.get("lengths"), meta.get("units")
    if not _strings(lengths) or not lengths:
        errors["lengths"] = "A non-empty list of video_length keys."
    elif not isinstance(units, dict) or any(
        not isinstance(units.get(key), int) or isinstance(units.get(key), bool) or units[key] < 1
        for key in lengths
    ):
        errors["units"] = "A whole number of units, at least 1, for every length."
    for field in ("keyframe_prompt", "motion_prompt"):
        value = meta.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > PROMPT_MAX:
            errors[field] = f"Text of at most {PROMPT_MAX} characters."
    script = meta.get("script")
    if not isinstance(script, dict) or any(
        not isinstance(script.get(part), str) for part in ("hook", "body", "cta")
    ):
        errors["script"] = "An object with hook, body and cta text."
    for field in ("beats", "hooks", "locations"):
        if not _strings(meta.get(field)) or not meta.get(field):
            errors[field] = "A non-empty list of text."
    if meta.get("needs", "") not in NEEDS:
        errors["needs"] = "Blank or app_screenshot."
    shots = meta.get("shots")
    if shots is not None:
        if (
            not isinstance(shots, list)
            or not SHOTS_MIN <= len(shots) <= SHOTS_MAX
            or any(
                not isinstance(shot, dict)
                or shot.get("use") not in SHOT_USES
                or not isinstance(shot.get("text", ""), str)
                for shot in shots
            )
        ):
            errors["shots"] = (
                f"{SHOTS_MIN} to {SHOTS_MAX} shots, each {{use: frame, photo or screen, text}}."
            )
        elif any(shot["use"] == "screen" for shot in shots) and meta.get("needs") != (
            "app_screenshot"
        ):
            errors["needs"] = "A screen shot needs app_screenshot."
    if not isinstance(meta.get("available", True), bool):
        errors["available"] = "true or false."
    if errors:
        raise DjangoValidationError(
            {"metadata": [f"{field}: {message}" for field, message in errors.items()]}
        )


def is_on(workspace: Workspace) -> bool:
    return flag_enabled(workspace.organization, AD_TEMPLATES_S17)


def require_on(workspace: Workspace) -> None:
    """Flag off is pre-step behaviour: the surfaces do not exist (404)."""
    if not is_on(workspace):
        raise NotFound


def references_cap(workspace: Workspace, entitlements: Entitlements | None = None) -> int | None:
    """How many product photos a render may send (`max_references`), never
    above the pipeline's own ceiling. `None` with the flag off: every photo, as
    before. A plan without the key keeps the ceiling, so a legacy plan loses
    nothing it had."""
    if not is_on(workspace):
        return None
    value = (entitlements or entitlements_for(workspace)).int_feature("max_references")
    return min(value, references.MAX_REFERENCES) if value >= 1 else references.MAX_REFERENCES


def _rows() -> list[CreativeOption]:
    return list(CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, is_active=True))


def _lengths() -> dict[str, CreativeOption]:
    return {
        row.key: row
        for row in CreativeOption.objects.filter(kind=CreativeKind.VIDEO_LENGTH, is_active=True)
    }


def feature_for(row: CreativeOption, *, with_person: bool) -> str:
    tier = row.metadata["tier"]
    if tier == "product" and with_person:
        tier = "people"
    return TIER_FEATURE[tier]


def shots_of(row: CreativeOption) -> list[dict[str, Any]]:
    """The template's shots; empty for a one-shot template (one clip)."""
    return list(row.metadata.get("shots") or [])


def has_frame(row: CreativeOption) -> bool:
    """Whether the template opens on a drawn first frame (a keyframe)."""
    shots = shots_of(row)
    return not shots or any(shot["use"] == "frame" for shot in shots)


def has_screenshot(product: Product, photo_ids: set[str] | None = None) -> bool:
    """`photo_ids` are the product's photo pks as strings, when already read."""
    tags = product.reference_tags or {}
    if photo_ids is None:
        photo_ids = {str(pk) for pk in product.reference_images.values_list("pk", flat=True)}
    return any(tag == SCREEN_TAG and media in photo_ids for media, tag in tags.items())


def fit_of(
    row: CreativeOption, product: Product | None, *, photo_ids: set[str] | None = None
) -> str | None:
    """Why this template cannot be made for this product, or `None`."""
    meta = row.metadata
    if product is not None and product.offer_kind and product.offer_kind not in meta["fits"]:
        return "template_fit"
    if meta.get("needs") == "app_screenshot" and (
        product is None or not has_screenshot(product, photo_ids)
    ):
        return "needs_screenshot"
    return None


FIT_MESSAGES = {
    "template_fit": "This template doesn't fit this product.",
    "needs_screenshot": "Needs an app screenshot: tag one of the product's photos as App screen.",
}


# -----------------------------------------------------------------------------
# The gallery
# -----------------------------------------------------------------------------
def gallery(workspace: Workspace, product: Product | None) -> dict[str, Any]:
    """Every active template with its fit and its lock, for this product.

    A template that does not fit the product's kind is still returned (with
    `fit`), so the page can say *why* a rail is empty rather than look broken;
    the page decides what to hide.
    """
    entitlements = entitlements_for(workspace)
    plan = entitlements.plan
    lengths = _lengths()
    # One upgrade answer per tier feature, not one Plan query per tile.
    upgrade = {feature: suggest_upgrade(plan, feature) for feature in TIER_FEATURE.values()}
    photo_ids = (
        {str(pk) for pk in product.reference_images.values_list("pk", flat=True)}
        if product is not None
        else set()
    )
    out = []
    for row in _rows():
        meta = row.metadata
        feature = TIER_FEATURE[meta["tier"]]
        locked = not entitlements.feature(feature)
        out.append(
            {
                "key": row.key,
                "label": row.label,
                "description": row.description,
                "category": meta["category"],
                "fits": meta["fits"],
                "person": meta["person"],
                "tier": meta["tier"],
                "lengths": [
                    {
                        "key": key,
                        "label": lengths[key].label,
                        "seconds": int(lengths[key].metadata.get("seconds", 0)),
                        "units": int(meta["units"][key]),
                    }
                    for key in meta["lengths"]
                    if key in lengths
                ],
                "beats": meta["beats"],
                "script": meta["script"],
                "hooks": meta["hooks"],
                "locations": meta["locations"],
                "needs": meta.get("needs", ""),
                "available": bool(meta.get("available", True)),
                "frame": has_frame(row),
                "fit": fit_of(row, product, photo_ids=photo_ids),
                "lock": (
                    {"feature": feature, "suggested_plan": upgrade[feature]} if locked else None
                ),
                # A person on an optional template is a `people` render.
                "person_lock": (
                    {
                        "feature": TIER_FEATURE["people"],
                        "suggested_plan": upgrade[TIER_FEATURE["people"]],
                    }
                    if meta["person"] == "optional"
                    and not entitlements.feature(TIER_FEATURE["people"])
                    else None
                ),
            }
        )
    return {
        "templates": out,
        "categories": [{"key": key, "label": label} for key, label in _category_labels()],
        "locations": [{"key": key, "label": key.capitalize()} for key in _location_keys(out)],
        "rerolls": entitlements.int_feature("ad_rerolls"),
        "references_cap": references_cap(workspace, entitlements),
        "references_total": len(photo_ids),
    }


def _category_labels() -> list[tuple[str, str]]:
    from ai.ad_template_seed import CATEGORIES as LABELS

    return LABELS


def _location_keys(templates: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(key for row in templates for key in row["locations"]))


# -----------------------------------------------------------------------------
# One template, at the point of use
# -----------------------------------------------------------------------------
def resolve(
    workspace: Workspace,
    key: str,
    *,
    product: Product | None,
    with_person: bool,
) -> CreativeOption:
    """The row, or the first reason it cannot be made. Raised in the order the
    person can act on: what it is, whether it fits, whether it is ready,
    whether the plan has it."""
    row = CreativeOption.objects.filter(
        kind=CreativeKind.AD_TEMPLATE, key=str(key), is_active=True
    ).first()
    if row is None:
        raise ValidationError({"template": f"'{key}' is not in the template catalogue."})
    if product is None:
        raise ValidationError({"product": "Pick the product this ad is for."})
    reason = fit_of(row, product)
    if reason is not None:
        raise TemplateRefusedError(FIT_MESSAGES[reason], code=reason, detail={"template": row.key})
    meta = row.metadata
    if meta["person"] == "required" and not with_person:
        raise TemplateRefusedError(
            "This format needs a person: pick an avatar.", code="person_required"
        )
    if meta["person"] == "none" and with_person:
        raise TemplateRefusedError("Effects-only template: no person in frame.", code="no_person")
    if not meta.get("available", True):
        raise TemplateUnavailableError(detail={"template": row.key})
    entitlements = entitlements_for(workspace)
    feature = feature_for(row, with_person=with_person)
    if not entitlements.feature(feature):
        raise FeatureNotAvailable(
            "Your plan does not include this template.",
            detail={"feature": feature, "template": row.key, "plan": entitlements.plan.code},
            suggested_plan=suggest_upgrade(entitlements.plan, feature),
        )
    return row


def speaks(row: CreativeOption) -> bool:
    """A talking format: the avatar must speak the content language."""
    return bool(row.metadata["tier"] == "talking")


def resolve_with_avatar(
    workspace: Workspace,
    key: str,
    *,
    product: Product | None,
    avatar_id: Any,
    language: str = "",
) -> tuple[CreativeOption, Any]:
    """The template and the person in it, each checked once: the avatar may be
    used, the template fits and is in the plan with or without a person, and —
    only for a format where the person speaks — the avatar speaks `language`."""
    avatar = None
    if avatar_id is not None:
        from brand.services import avatars

        avatar = avatars.resolve_for_render(workspace, avatar_id)
    row = resolve(workspace, key, product=product, with_person=avatar is not None)
    if avatar is not None and speaks(row):
        from brand.services import avatars

        avatars.require_language(avatar, language)
    return row, avatar


def length_units(row: CreativeOption, length_key: str) -> int:
    if length_key not in row.metadata["lengths"]:
        raise TemplateRefusedError(
            "This template does not come in that length.",
            code="template_length",
            detail={"lengths": row.metadata["lengths"]},
        )
    return int(row.metadata["units"][length_key])


def _location(row: CreativeOption, location: str) -> str:
    from ai.ad_template_seed import LOCATIONS

    allowed = row.metadata["locations"]
    chosen = location or allowed[0]
    if chosen not in allowed:
        raise TemplateRefusedError(
            f"'{location}' is not a location of this template.",
            code="template_location",
            detail={"locations": allowed},
        )
    return LOCATIONS.get(chosen, chosen)


def keyframe_prompt(
    row: CreativeOption, *, product: Product, location: str = "", person: str = ""
) -> str:
    """The first frame, filled. `person` is the avatar's description or blank."""
    prompt = str(row.metadata["keyframe_prompt"])
    optional = f" {person} is in the frame, natural and unposed." if person else ""
    return (
        prompt.replace("{product}", product.name)
        .replace("{location}", _location(row, location))
        .replace("{person_optional}", optional)
        .replace("{person}", person or "A person")
    )


def motion_prompt(row: CreativeOption) -> str:
    return str(row.metadata["motion_prompt"])


def script_parts(
    row: CreativeOption, script: dict[str, Any] | None, product: Product
) -> dict[str, str]:
    """Hook, body and call to action: the person's words, or the row's, filled."""
    base = row.metadata["script"]
    out = {}
    for part in ("hook", "body", "cta"):
        value = (script or {}).get(part)
        text = value if isinstance(value, str) else base[part]
        out[part] = text.replace("{product}", product.name).strip()[:SCRIPT_MAX]
    return out


def script_text(row: CreativeOption, script: dict[str, Any] | None, product: Product) -> str:
    """The post caption a render carries: the person's script, or the row's."""
    return "\n".join(text for text in script_parts(row, script, product).values() if text)


def shot_line(text: str, product: Product, script: dict[str, str]) -> str:
    """A shot's kinetic line: `{product}`, `{hook}`, `{body}`, `{cta}` filled."""
    for key, value in (("product", product.name), *script.items()):
        text = text.replace("{" + key + "}", value)
    return text.strip()


# -----------------------------------------------------------------------------
# Keyframes and re-rolls
# -----------------------------------------------------------------------------
def check_reroll(workspace: Workspace, root: Generation) -> None:
    """A re-roll is a model call: the nth beyond `ad_rerolls` is a 402. Hand
    edits to the script are never counted — they never reach this."""
    entitlements = entitlements_for(workspace)
    limit = entitlements.int_feature("ad_rerolls")
    used = Generation.objects.filter(
        workspace=workspace, creative__ad_template__root=root.pk
    ).count()
    if used >= limit:
        raise RerollLimitError(
            f"This ad has used its {limit} re-roll{'s' if limit != 1 else ''}.",
            detail={"feature": "ad_rerolls", "limit": limit, "used": used},
            suggested_plan=suggest_upgrade(entitlements.plan, "ad_rerolls"),
        )


def create_keyframe(
    workspace: Workspace,
    *,
    user: Any,
    row: CreativeOption,
    product: Product,
    avatar: Any = None,
    location: str = "",
    reroll_of: Generation | None = None,
) -> Generation:
    """The first frame as a PENDING image generation; the caller queues it.

    The avatar's portrait travels as a `person` asset — never to the quality
    gate, which holds the *product* to its photos and must not score a face.
    """
    if not has_frame(row):
        raise TemplateRefusedError(
            "This template has no first frame: it is made from the product's own photos.",
            code="no_frame",
        )
    from ai.services import pipeline

    root: Generation | None = None
    if reroll_of is not None:
        marker = (reroll_of.creative or {}).get("ad_template") or {}
        if marker.get("template") is None:
            raise ValidationError({"reroll_of": "That generation is not a template keyframe."})
        root = Generation.objects.filter(
            workspace=workspace, pk=marker.get("root") or reroll_of.pk
        ).first()
        if root is None:
            raise NotFound
        check_reroll(workspace, root)

    person = ""
    assets: list[dict[str, Any]] = []
    if avatar is not None:
        from brand.services import avatars

        person = avatars.describe(avatar)
        assets = [{"media": avatar.portrait_id, "role": "person", "note": ""}]
    marker = {"template": row.key, "location": location, "avatar": avatar.pk if avatar else None}
    if root is not None:
        marker["root"] = root.pk
    return pipeline.create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt=keyframe_prompt(row, product=product, location=location, person=person),
        product=product,
        aspect=KEYFRAME_ASPECT,
        creative={
            **({"assets": assets} if assets else {}),
            "ad_template": marker,
            **({"avatar": avatar.pk} if avatar else {}),
        },
    )


# -----------------------------------------------------------------------------
# The script writer (S17-13, `AD_SCRIPT`)
# -----------------------------------------------------------------------------
LANGUAGE_NAMES = {"en": "English", "fr": "French", "it": "Italian"}
SCRIPT_SYSTEM = (
    "You write the script for a short vertical video ad from a template. "
    "Three parts: a hook spoken or shown in the first two seconds, a body, and a call to "
    "action. Keep the template's shape and beats; make the words specific to the product.\n"
    "Rules:\n"
    "- Write in the language asked for, and nothing else.\n"
    "- Never invent a fact, a number, a price, a rating or a claim the brief does not give.\n"
    "- Short spoken sentences; the whole script must be readable in the seconds given.\n"
    "- No hashtags, no emoji, no links.\n"
    "Reply with exactly three lines and nothing else:\n"
    "HOOK: …\nBODY: …\nCTA: …"
)
_SCRIPT_LINE = re.compile(r"^\s*(HOOK|BODY|CTA)\s*:\s*(.+?)\s*$", re.I | re.M)


def _script_from(body: str, brief: str) -> dict[str, str] | None:
    """The three parts, or `None` when the reply is not the three lines asked
    for or fails the rephrase checks (a number the brief never gave, a link)."""
    from ai.services import rephrase

    found = {key.lower(): text for key, text in _SCRIPT_LINE.findall(body)}
    parts = {key: found.get(key, "")[:SCRIPT_MAX] for key in ("hook", "body", "cta")}
    if not all(parts.values()) or not rephrase.acceptable(" ".join(parts.values()), brief):
        return None
    return parts


def write_script(
    workspace: Workspace, *, key: str, product: Product, language: str, seconds: int = 0
) -> dict[str, Any]:
    """The template's script, written for this product by the text model.

    Synchronous, like the Tools suite: one short call. Paid in credits (the
    `(TEXT, RECIPE)` price) and debited only when the words pass their checks;
    a reply that fails them, or a model that is down, gives back the
    template's own words, free (`by: "template"`)."""
    from django.db import transaction

    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider
    from ai.services.costing import resolve_cost
    from billing.services import ledger
    from common.exceptions import ProviderError

    peek = CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key=str(key)).first()
    row = resolve(
        workspace,
        key,
        product=product,
        with_person=bool(peek and peek.metadata.get("person") == "required"),
    )
    if language and language not in LANGUAGE_NAMES:
        raise ValidationError({"language": "One of en, fr, it."})
    price = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.RECIPE)
    entitlements = entitlements_for(workspace)
    entitlements.require_credits(price)

    base = script_parts(row, None, product)
    meta = row.metadata
    brief = "\n".join(
        line
        for line in (
            f"Product: {product.name}",
            f"About it: {product.short_description}" if product.short_description else "",
            f"Template: {row.label} — beats: {' · '.join(meta['beats'])}",
            f"Seconds: {seconds}" if seconds else "",
            f"Language: {LANGUAGE_NAMES.get(language or 'en', 'English')}",
            f"Starting hook: {base['hook']}",
            f"Starting body: {base['body']}",
            f"Starting call to action: {base['cta']}",
        )
        if line
    )
    parts = None
    try:
        result = get_text_provider(AITask.AD_SCRIPT, workspace=workspace).generate(
            system=SCRIPT_SYSTEM, prompt=brief, n=1
        )
        parts = _script_from(result.variants[0].body if result.variants else "", brief)
    except ProviderError:
        parts = None
    if parts is None:
        return {**base, "by": "template", "credits": 0}
    with transaction.atomic():
        ledger.debit_credits(
            workspace,
            price,
            quota=entitlements.quota("monthly_ai_credits"),
            note=f"ad script {row.key}",
        )
    return {**parts, "by": "model", "credits": price}
