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
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import NotFound, ValidationError

from ai.ad_template_seed import CAPTURES, GOALS
from ai.models import CreativeKind, CreativeOption, Generation, GenerationKind, GenerationMode
from ai.services import claims, references, template_results
from billing.services.entitlements import Entitlements, entitlements_for
from billing.services.flags import AD_TEMPLATES_S17, flag_enabled
from billing.services.plans import suggest_upgrade
from brand.services.kit import kit_for
from common.exceptions import FeatureNotAvailable, OCCSError, StateConflict
from products.brief import identity_photos
from products.models import Product
from workspaces.models import Workspace

CATEGORIES = (
    "text",
    "unbox",
    "hand",
    "tryon",
    "use",
    "app",
    "tour",
    "maker",
    "talk",
    "interview",
    "promo",
    "fx",
)
OFFER_KINDS = ("physical", "beauty", "app", "service", "space")
PERSON = ("required", "optional", "none")
#: Which plan feature unlocks each tier.
TIER_FEATURE = {
    "product": "ad_templates",
    "people": "ad_templates_people",
    "talking": "ad_templates_talking",
}
#: A premium template (`premium: true`) also needs this feature in the plan,
#: or the add-on of the same key, which the `templates-premium` pack grants.
PREMIUM_FEATURE = "ad_templates_premium"
PREMIUM_PACK = "templates-premium"
#: How many people and products one ad may show, each counting the main one.
#: A plan without the key keeps one of each, as before.
CAST_FEATURES = {"avatars": "ad_avatars_per_ad", "products": "ad_products_per_ad"}
#: What a template needs beyond the product (S17-33): an app screenshot, a real
#: offer and its end date (asked at render), three variants' photos, four
#: photos for a grid, or the brand's own place photos.
NEEDS = ("", "app_screenshot", "offer", "variants", "photos", "places", "reviews")
VARIANTS_MIN = 3
GRID_PHOTOS_MIN = 4
#: A multi-shot template's shots (T3): `frame` animates the first frame,
#: `photo` animates the product's next photo, `screen` sets its app screenshot
#: in as a still — never through the video model, which would blur it. S17-33
#: adds shots drawn in code (`ai.services.graphics`): `card` (words on the
#: brand colour), `cutout` (the next photo on a colour field), `grid` (every
#: photo at once), `chat` (the hook and body as bubbles); and `place`, the
#: brand's own place photo, animated.
SHOT_USES = ("frame", "photo", "screen", "card", "cutout", "grid", "chat", "place")
DRAWN_USES = frozenset({"card", "cutout", "grid", "chat"})
TRANSITIONS = ("crossfade", "slide", "cut")
OFFER_TEXT_MAX = 60
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


class CastLimitError(FeatureNotAvailable):
    default_code = "cast_limit_reached"
    default_detail = "Your plan does not allow that many people or products in one ad."


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
        errors["needs"] = f"Blank or one of {', '.join(n for n in NEEDS if n)}."
    shots = meta.get("shots")
    if shots is not None:
        if (
            not isinstance(shots, list)
            or not SHOTS_MIN <= len(shots) <= SHOTS_MAX
            or any(
                not isinstance(shot, dict)
                or shot.get("use") not in SHOT_USES
                or not isinstance(shot.get("text", ""), str)
                or not isinstance(shot.get("kicker", ""), str)
                or not isinstance(shot.get("strike", False), bool)
                for shot in shots
            )
        ):
            errors["shots"] = (
                f"{SHOTS_MIN} to {SHOTS_MAX} shots, each {{use: one of "
                f"{', '.join(SHOT_USES)}, text, kicker?, strike?}}."
            )
        elif any(shot["use"] == "screen" for shot in shots) and meta.get("needs") != (
            "app_screenshot"
        ):
            errors["needs"] = "A screen shot needs app_screenshot."
    if not isinstance(meta.get("available", True), bool):
        errors["available"] = "true or false."
    if not isinstance(meta.get("premium", False), bool):
        errors["premium"] = "true or false."
    if "goal" in meta and meta["goal"] not in GOALS:
        errors["goal"] = f"One of {', '.join(GOALS)}."
    if not isinstance(meta.get("hidden", False), bool):
        errors["hidden"] = "true or false."
    if not isinstance(meta.get("protect", ""), str) or len(meta.get("protect", "")) > PROMPT_MAX:
        errors["protect"] = f"Text of at most {PROMPT_MAX} characters."
    if meta.get("transition", "crossfade") not in TRANSITIONS:
        errors["transition"] = f"One of {', '.join(TRANSITIONS)}."
    if not isinstance(meta.get("script_hint", ""), str):
        errors["script_hint"] = "Text."
    for field in ("preview", "poster"):
        if not isinstance(meta.get(field, ""), str):
            errors[field] = "A path or URL."
    if not isinstance(meta.get("land", False), bool):
        errors["land"] = "true or false."
    if "capture" in meta and meta["capture"] not in CAPTURES:
        errors["capture"] = f"One of {', '.join(CAPTURES)}."
    if not isinstance(meta.get("negative", ""), str) or len(meta.get("negative", "")) > PROMPT_MAX:
        errors["negative"] = f"Text of at most {PROMPT_MAX} characters."
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


def premium_open(entitlements: Entitlements) -> bool:
    """The plan has premium templates, or the organization bought the pack."""
    return bool(entitlements.feature(PREMIUM_FEATURE)) or entitlements.has_addon(PREMIUM_FEATURE)


def premium_pack() -> str | None:
    """The pack a locked premium tile offers, when one is on sale."""
    from billing.models import Pack, PackKind

    pack = Pack.objects.filter(code=PREMIUM_PACK, kind=PackKind.TEMPLATE, is_public=True).first()
    return pack.code if pack else None


def cast_limits(entitlements: Entitlements) -> dict[str, int]:
    return {part: max(1, entitlements.int_feature(key, 1)) for part, key in CAST_FEATURES.items()}


#: A product's campaign goal (`Product.campaign_goal`) → the template goal the
#: gallery recommends first.
GOAL_FOR_CAMPAIGN = {"sales": "sell", "traffic": "explain", "awareness": "show"}
RECOMMENDED = 3


def _recommend(templates: list[dict[str, Any]], product: Product | None) -> list[str]:
    """Up to three templates for this product (S17-32): ones it can make now,
    the campaign's goal first, then the ones made for this kind of product,
    each from a different kind of template."""
    want = GOAL_FOR_CAMPAIGN.get(product.campaign_goal) if product is not None else None
    kind = product.offer_kind if product is not None else ""
    makeable = [
        row for row in templates if row["available"] and row["fit"] is None and row["lock"] is None
    ]

    def proven(row: dict[str, Any]) -> float:
        # S17-37: within a goal, this brand's own results lift a template — only
        # one measured often enough to go on, and the better its median the more.
        results = row.get("results") or {}
        return -(results.get("median_engagement") or 0.0) if results.get("enough") else 0.0

    ranked = sorted(
        makeable,
        key=lambda row: (
            row["goal"] != want,
            proven(row),
            not (kind and kind in row["fits"] and len(row["fits"]) <= 2),
        ),
    )
    picked: list[str] = []
    categories: set[str] = set()
    for row in ranked:
        if row["category"] in categories:
            continue
        picked.append(row["key"])
        categories.add(row["category"])
        if len(picked) == RECOMMENDED:
            break
    return picked


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
    needs = meta.get("needs")
    if needs == "app_screenshot" and (product is None or not has_screenshot(product, photo_ids)):
        return "needs_screenshot"
    if needs in ("variants", "photos"):
        tags = (product.reference_tags or {}) if product is not None else {}
        if photo_ids is None and product is not None:
            photo_ids = {str(pk) for pk in product.reference_images.values_list("pk", flat=True)}
        plain = [pk for pk in photo_ids or set() if tags.get(pk) != SCREEN_TAG]
        if len(plain) < (VARIANTS_MIN if needs == "variants" else GRID_PHOTOS_MIN):
            return "needs_variants" if needs == "variants" else "needs_photos"
    if needs == "places" and (product is None or not _has_places(product.workspace)):
        return "needs_places"
    if needs == "reviews" and (product is None or consented_review(product) is None):
        return "needs_reviews"
    return None


def consented_review(product: Product) -> dict[str, Any] | None:
    """The product's first review given with the customer's consent (S17-36);
    a review without it is never shown."""
    for review in product.reviews or []:
        if isinstance(review, dict) and review.get("consent") is True and review.get("quote"):
            return review
    return None


def _has_places(workspace: Workspace) -> bool:
    from brand.models import BrandPlace, PlaceSource

    return BrandPlace.objects.filter(
        workspace=workspace, source=PlaceSource.UPLOAD, media__isnull=False
    ).exists()


FIT_MESSAGES = {
    "template_fit": "This template doesn't fit this product.",
    "needs_screenshot": "Needs an app screenshot: tag one of the product's photos as App screen.",
    "needs_variants": f"Needs photos of at least {VARIANTS_MIN} variants of the product.",
    "needs_photos": f"Needs at least {GRID_PHOTOS_MIN} photos of the product.",
    "needs_places": "Needs a photo of your own place: add one at the Scene step.",
    "needs_reviews": (
        "Needs a customer review given with their consent: add one on the product page."
    ),
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
    from brand.models import PlaceSource
    from brand.services import places

    entitlements = entitlements_for(workspace)
    plan = entitlements.plan
    lengths = _lengths()
    # One upgrade answer per tier feature, not one Plan query per tile.
    upgrade = {
        feature: suggest_upgrade(plan, feature)
        for feature in (*TIER_FEATURE.values(), PREMIUM_FEATURE)
    }
    premium = premium_open(entitlements)
    pack = None if premium else premium_pack()
    photo_ids = (
        {str(pk) for pk in product.reference_images.values_list("pk", flat=True)}
        if product is not None
        else set()
    )
    results = template_results.results_for(workspace)
    out = []
    for row in _rows():
        meta = row.metadata
        if meta.get("hidden"):
            continue
        feature = TIER_FEATURE[meta["tier"]]
        lock = None
        if not entitlements.feature(feature):
            lock = {"feature": feature, "suggested_plan": upgrade[feature], "pack": None}
        elif meta.get("premium") and not premium:
            lock = {
                "feature": PREMIUM_FEATURE,
                "suggested_plan": upgrade[PREMIUM_FEATURE],
                "pack": pack,
            }
        out.append(
            {
                "key": row.key,
                "label": row.localized("label"),
                "description": row.localized("description"),
                "category": meta["category"],
                "goal": meta.get("goal") or "show",
                # S17-37: what it did for this brand, or null before any post.
                "results": results.get(row.key),
                "fits": meta["fits"],
                "person": meta["person"],
                "tier": meta["tier"],
                "lengths": [
                    {
                        "key": key,
                        "label": lengths[key].localized("label"),
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
                "premium": bool(meta.get("premium", False)),
                "preview": str(meta.get("preview") or ""),
                "poster": str(meta.get("poster") or ""),
                "frame": has_frame(row),
                "fit": fit_of(row, product, photo_ids=photo_ids),
                "lock": lock,
                # A person on an optional template is a `people` render.
                "person_lock": (
                    {
                        "feature": TIER_FEATURE["people"],
                        "suggested_plan": upgrade[TIER_FEATURE["people"]],
                        "pack": None,
                    }
                    if meta["person"] == "optional"
                    and not entitlements.feature(TIER_FEATURE["people"])
                    else None
                ),
            }
        )
    return {
        "templates": out,
        "recommended": _recommend(out, product),
        "goals": [{"key": goal, "label": goal} for goal in GOALS],
        "categories": [{"key": key, "label": label} for key, label in _category_labels()],
        "locations": _places(_location_keys(out)),
        # S17-28: the brand's own places (any template can be set there), the
        # catalogue places it keeps a plate for, and what keeping one costs.
        "own_places": [
            {"key": f"{places.OWN}{place.pk}", "label": place.label, "image": place.media.file.url}
            for place in places.list_places(workspace)
            if place.source == PlaceSource.UPLOAD and place.media is not None
        ],
        "kept": places.kept_keys(workspace),
        # S17-34: what the script writer can be asked to argue, and how.
        "angles": [
            {
                "key": row.key,
                "label": row.localized("label"),
                "description": row.localized("description"),
                "stage": str((row.metadata or {}).get("stage") or ""),
                "families": list((row.metadata or {}).get("families") or []),
            }
            for row in CreativeOption.objects.filter(kind=CreativeKind.ANGLE, is_active=True)
        ],
        "hook_families": [
            {
                "key": row.key,
                "label": row.localized("label"),
                "description": row.localized("description"),
            }
            for row in CreativeOption.objects.filter(kind=CreativeKind.HOOK_FAMILY, is_active=True)
        ],
        "keep_credits": places.keep_credits(workspace),
        "rerolls": entitlements.int_feature("ad_rerolls"),
        "prompt_edit": bool(entitlements.feature("ad_prompt_edit")),
        "avatars_per_ad": cast_limits(entitlements)["avatars"],
        "products_per_ad": cast_limits(entitlements)["products"],
        "references_cap": references_cap(workspace, entitlements),
        "references_total": len(photo_ids),
    }


def _category_labels() -> list[tuple[str, str]]:
    from ai.ad_template_seed import CATEGORIES as LABELS

    return LABELS


def _location_keys(templates: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(key for row in templates for key in row["locations"]))


def _places(keys: list[str]) -> list[dict[str, str]]:
    """Each place the templates use, named in the interface language, with its
    picture; a key with no row reads its own name and no picture."""
    rows = {
        row.key: row for row in CreativeOption.objects.filter(kind=CreativeKind.PLACE, key__in=keys)
    }
    return [
        {
            "key": key,
            "label": rows[key].localized("label") if key in rows else key.capitalize(),
            "image": str((rows[key].metadata or {}).get("image") or "") if key in rows else "",
        }
        for key in keys
    ]


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
    if meta.get("premium") and not premium_open(entitlements):
        raise FeatureNotAvailable(
            "This is a premium template: upgrade, or buy the template pack.",
            detail={"feature": PREMIUM_FEATURE, "template": row.key, "pack": premium_pack()},
            suggested_plan=suggest_upgrade(entitlements.plan, PREMIUM_FEATURE),
        )
    return row


def _plan_for(entitlements: Entitlements, key: str, needed: int) -> str:
    """The cheapest public plan above this one that allows `needed` of `key`."""
    from billing.models import Plan

    current = entitlements.plan.price_monthly_cents
    for plan in Plan.objects.filter(is_public=True, price_monthly_cents__gt=current).order_by(
        "price_monthly_cents", "sort_order", "id"
    ):
        value = plan.feature(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= needed:
            return str(plan.code)
    return suggest_upgrade(entitlements.plan, key)


def resolve_cast(
    workspace: Workspace,
    *,
    avatar: Any,
    product: Product,
    extra_avatars: list[int],
    extra_products: list[int],
) -> tuple[list[Any], list[Product]]:
    """The people and products beside the main ones, each checked: none twice,
    no more than the plan's `ad_avatars_per_ad` / `ad_products_per_ad`
    (counting the main one), every one this brand's and usable now."""
    if extra_avatars and avatar is None:
        raise ValidationError({"extra_avatars": "Pick the main avatar first."})
    avatar_ids = [avatar.pk, *extra_avatars] if avatar is not None else []
    product_ids = [product.pk, *extra_products]
    for field, ids in (("extra_avatars", avatar_ids), ("extra_products", product_ids)):
        if len(set(ids)) != len(ids):
            raise ValidationError({field: "The same one is in the ad twice."})
    entitlements = entitlements_for(workspace)
    limits = cast_limits(entitlements)
    for part, ids in (("avatars", avatar_ids), ("products", product_ids)):
        if len(ids) > limits[part]:
            key = CAST_FEATURES[part]
            raise CastLimitError(
                f"Your plan allows {limits[part]} {part[:-1]}"
                f"{'s' if limits[part] != 1 else ''} in one ad.",
                detail={"feature": key, "limit": limits[part], "asked": len(ids)},
                suggested_plan=_plan_for(entitlements, key, len(ids)),
            )
    people = []
    if extra_avatars:
        from brand.services import avatars

        people = [avatars.resolve_for_render(workspace, pk) for pk in extra_avatars]
    found = Product.objects.filter(workspace=workspace, pk__in=extra_products).in_bulk()
    if len(found) != len(extra_products):
        raise NotFound
    products = [found[pk] for pk in extra_products]
    if any(not item.reference_images.exists() for item in products):
        raise ValidationError({"extra_products": "Every product in the ad needs a photo."})
    return people, products


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


@dataclass(frozen=True)
class Place:
    """Where a template is set (S17-27): what the camera sees, the light it is
    locked to, where the product stands, and what it must never show."""

    set: str
    light: str = ""
    zone: str = ""
    forbid: str = ""


def place_text(scene: str, workspace: Workspace | None) -> str:
    """A place's set with the brand-colour placeholder filled: the kit's
    primary, or a calm neutral when there is no kit."""
    from ai.ad_template_seed import BRAND_COLOUR

    if BRAND_COLOUR not in scene:
        return scene
    kit = kit_for(workspace) if workspace is not None else None
    primary = dict(kit.colors).get("Primary") if kit else None
    return scene.replace(
        BRAND_COLOUR, f"the brand colour {primary}" if primary else "a calm neutral colour"
    )


#: The brand's own place (S17-28): its photo carries the set and the light.
OWN_PLACE = Place(
    set="the brand's own place, exactly as in the attached place photo",
    zone="where it would really be used in that place",
    forbid="no readable text or signs, no other brand's logo",
)


def place_of(row: CreativeOption, location: str, *, workspace: Workspace | None = None) -> Place:
    """The template's place: the person's, or the template's first. 400 for a
    place the template does not offer. A key with no `place` row reads the
    words it had before places were rows. The brand's own place (`own:<id>`)
    fits every template; another brand's is a 404."""
    from ai.ad_template_seed import LOCATIONS
    from brand.services import places

    allowed = row.metadata["locations"]
    chosen = location or allowed[0]
    if chosen.startswith(places.OWN):
        if workspace is None:
            raise TemplateRefusedError(
                "Your own place needs the brand it belongs to.", code="template_location"
            )
        places.own(workspace, chosen)
        return OWN_PLACE
    if chosen not in allowed:
        raise TemplateRefusedError(
            f"'{location}' is not a location of this template.",
            code="template_location",
            detail={"locations": allowed},
        )
    found = CreativeOption.objects.filter(
        kind=CreativeKind.PLACE, key=chosen, is_active=True
    ).first()
    if found is None:
        return Place(set=LOCATIONS.get(chosen, chosen))
    meta = found.metadata or {}
    scene = str(meta.get("set") or found.prompt_fragment or chosen)
    return Place(
        set=place_text(scene, workspace),
        light=str(meta.get("light") or ""),
        zone=str(meta.get("zone") or ""),
        forbid=str(meta.get("forbid") or ""),
    )


def place_forbid(row: CreativeOption, location: str, workspace: Workspace | None = None) -> str:
    """What a render's place forbids, or "" when the place is no longer the
    template's, or gone — a render already paid for is never failed over its
    words."""
    try:
        return place_of(row, location, workspace=workspace).forbid
    except (TemplateRefusedError, NotFound):
        return ""


def place_photo(row: CreativeOption, location: str, workspace: Workspace) -> int | None:
    """The picture that sets the scene (S17-28): the brand's own place, or its
    kept plate for this catalogue place; None when there is neither."""
    from brand.services import places

    chosen = location or row.metadata["locations"][0]
    if chosen.startswith(places.OWN):
        return places.own(workspace, chosen).media_id
    plate = places.plate(workspace, chosen)
    return plate.media_id if plate is not None else None


def keyframe_prompt(
    row: CreativeOption,
    *,
    product: Product,
    location: str = "",
    person: str = "",
    also: tuple[str, ...] = (),
    workspace: Workspace | None = None,
    own_light: bool = False,
) -> str:
    """The first frame, filled. `person` is the avatar's description or blank;
    `also` names the other products in the ad. The place's light is left out
    when the person chose their own (`own_light`): theirs wins."""
    prompt = str(row.metadata["keyframe_prompt"])
    optional = f" {person} is in the frame, natural and unposed." if person else ""
    place = place_of(row, location, workspace=workspace)
    setting = "; ".join(
        filter(
            None,
            [
                place.set,
                f"light: {place.light}" if place.light and not own_light else "",
                f"the product: {place.zone}" if place.zone else "",
            ],
        )
    )
    filled = (
        prompt.replace("{product}", " and ".join((product.name, *also)))
        .replace("{location}", setting)
        .replace("{person_optional}", optional)
        .replace("{person}", person or "A person")
    )
    # The one detail that kills the idea if lost (S17-32), word for word.
    protect = str(row.metadata.get("protect") or "").strip()
    return f"{filled} Protect: {protect}" if protect else filled


#: Beats the composer plays, never the video model.
CARD_BEATS = frozenset({"End card"})


def motion_prompt(row: CreativeOption, *, seconds: float | None = None) -> str:
    """The row's motion; with a length, followed by its beats on the clock
    (S17-31) — one call with internal beats drifts less than several."""
    motion = str(row.metadata["motion_prompt"])
    beats = [beat for beat in row.metadata.get("beats") or [] if beat not in CARD_BEATS]
    if not seconds or not beats:
        return motion
    step = seconds / len(beats)
    windows = "; ".join(
        f"{round(index * step):g}-{round((index + 1) * step):g} s: {beat}"
        for index, beat in enumerate(beats)
    )
    return f"{motion} Timing: {windows}."


def role_of(row: CreativeOption) -> str:
    """`end` for a template filmed towards its first frame (S17-31): the clip
    lands on the approved still, on the `video_end` route; `start` otherwise."""
    return "end" if row.metadata.get("land") else "start"


def negative_of(row: CreativeOption, forbid: str = "") -> str:
    """The template's negative, and what its place forbids (S17-27)."""
    return ", ".join(
        part for part in (str(row.metadata.get("negative") or "").strip(), forbid.strip()) if part
    )


def avoid_line(row: CreativeOption, forbid: str = "") -> str:
    """The negative as the last line of a prompt, or "". The routed models take
    no negative field (OpenRouter's `POST /videos` has none), so it travels as
    words — added after any rewrite, which may never drop it."""
    negative = negative_of(row, forbid)
    return f"Avoid: {negative}." if negative else ""


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


def shot_line(
    text: str, product: Product, script: dict[str, str], offer: dict[str, str] | None = None
) -> str:
    """A shot's line: `{product}`, `{hook}`, `{body}`, `{cta}` filled; the
    body's benefits as `{claim1}`…`{claim3}` (split on " · "); an offer's
    `{offer}` and `{ends}` (S17-33)."""
    claims_of_body = [part.strip() for part in script.get("body", "").split("·")]
    review = consented_review(product) or {}
    values = [
        ("review", str(review.get("quote") or "")),
        ("review_author", str(review.get("author") or "")),
        ("product", product.name),
        *script.items(),
        *((f"claim{index + 1}", claim) for index, claim in enumerate(claims_of_body)),
        ("offer", (offer or {}).get("text", "")),
        ("ends", (offer or {}).get("ends_label", "")),
    ]
    for key, value in values:
        text = text.replace("{" + key + "}", value)
    return text.strip()


class OfferRequiredError(TemplateRefusedError):
    default_code = "offer_required"
    default_detail = "This template needs the offer: what it is and the day it ends."


class OfferEndedError(TemplateRefusedError):
    default_code = "offer_ended"
    default_detail = "That offer has already ended."


_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "fr": (
        "janv.",
        "févr.",
        "mars",
        "avr.",
        "mai",
        "juin",
        "juil.",
        "août",
        "sept.",
        "oct.",
        "nov.",
        "déc.",
    ),
    "it": ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"),
}
_UNTIL = {"en": "Until {date}", "fr": "Jusqu'au {date}", "it": "Fino al {date}"}
#: A card's kicker in the ad's language (S17-33); anything else is shown as written.
KICKERS = {
    "Myth": {"fr": "Idée reçue", "it": "Falso mito"},
    "Fact": {"fr": "En vrai", "it": "In realtà"},
}


def kicker_text(kicker: str, language: str) -> str:
    return KICKERS.get(kicker, {}).get(language, kicker)


def clean_offer(raw: Any) -> dict[str, str]:
    """A real offer (S17-33): its words and the day it ends, which must not be
    past. Urgency only when it is true (sales `scarcity-urgency-calibration`)."""
    import datetime as dt

    from django.utils import timezone

    if not isinstance(raw, dict):
        raise OfferRequiredError
    text = " ".join(str(raw.get("text") or "").split())[:OFFER_TEXT_MAX]
    try:
        ends = dt.date.fromisoformat(str(raw.get("ends") or ""))
    except ValueError as error:
        raise OfferRequiredError from error
    if not text:
        raise OfferRequiredError
    if ends < timezone.localdate():
        raise OfferEndedError(detail={"ends": ends.isoformat()})
    return {"text": text, "ends": ends.isoformat()}


def ends_label(ends: str, language: str) -> str:
    """ "Until 30 Nov" in the ad's language, for an offer's card."""
    import datetime as dt

    day = dt.date.fromisoformat(ends)
    lang = language if language in _MONTHS else "en"
    return _UNTIL[lang].format(date=f"{day.day} {_MONTHS[lang][day.month - 1]}")


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


#: The longest free-text direction an ad may carry on top of its controls.
DIRECTION_MAX = 400


def clean_prompt(workspace: Workspace, raw: Any) -> str:
    """The person's own words for the render, or "". Writing any is a plan
    feature (`ad_prompt_edit`): 402 on a plan without it, never ignored."""
    text = " ".join(str(raw or "").split())[:DIRECTION_MAX]
    if text:
        entitlements_for(workspace).require_feature("ad_prompt_edit")
    return text


def create_keyframe(
    workspace: Workspace,
    *,
    user: Any,
    row: CreativeOption,
    product: Product,
    avatar: Any = None,
    location: str = "",
    reroll_of: Generation | None = None,
    extra_avatars: list[Any] | None = None,
    extra_products: list[Product] | None = None,
    look: dict[str, Any] | None = None,
    prompt: str = "",
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
        parent = (reroll_of.creative or {}).get("ad_template") or {}
        if parent.get("template") is None:
            raise ValidationError({"reroll_of": "That generation is not a template keyframe."})
        root = Generation.objects.filter(
            workspace=workspace, pk=parent.get("root") or reroll_of.pk
        ).first()
        if root is None:
            raise NotFound
        check_reroll(workspace, root)

    person = ""
    assets: list[dict[str, Any]] = []
    people = [avatar, *(extra_avatars or [])] if avatar is not None else []
    others = list(extra_products or [])
    if people:
        from brand.services import avatars

        person = " and ".join(avatars.describe(item) for item in people)
        assets = [{"media": item.portrait_id, "role": "person", "note": ""} for item in people]
    for item in others:
        photos = identity_photos(item)
        assert photos  # resolve_cast refused a product with no photo
        assets.append({"media": photos[0].pk, "role": "product", "note": f"This is {item.name}."})
    negative = negative_of(row, place_of(row, location, workspace=workspace).forbid)
    scene = place_photo(row, location, workspace)
    if scene is not None:
        assets.append({"media": scene, "role": "place", "note": ""})
    marker: dict[str, Any] = {
        "template": row.key,
        "location": location,
        "avatar": avatar.pk if avatar else None,
    }
    if people[1:]:
        marker["extra_avatars"] = [item.pk for item in people[1:]]
    if others:
        marker["extra_products"] = [item.pk for item in others]
    if root is not None:
        marker["root"] = root.pk
    return pipeline.create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt=" ".join(
            [
                keyframe_prompt(
                    row,
                    product=product,
                    location=location,
                    person=person,
                    also=tuple(item.name for item in others),
                    workspace=workspace,
                    own_light=bool((look or {}).get("light")),
                ),
                prompt,
            ]
        ).strip(),
        product=product,
        aspect=KEYFRAME_ASPECT,
        creative={
            **(look or {}),
            **({"assets": assets} if assets else {}),
            "ad_template": marker,
            **({"avatar": avatar.pk} if avatar else {}),
            # A keep-out rule (`creative.keep_out_lines`): sent verbatim after
            # the brief's rewrite, which may never drop it.
            **({"negative": negative} if negative else {}),
        },
    )


# -----------------------------------------------------------------------------
# The script writer (S17-13, `AD_SCRIPT`)
# -----------------------------------------------------------------------------
LANGUAGE_NAMES = {"en": "English", "fr": "French", "it": "Italian"}
#: The hook rules follow goose-skills `write-video-ad-script` (hook families,
#: dead openers) and marketingskills `ad-creative`; the dead openers and the
#: hook's length are also checked in code (`_script_from`), so a reply that
#: ignores them falls back to the template's own words, free.
DEAD_OPENERS = (
    "introducing",
    "meet ",
    "imagine",
    "picture this",
    "in a world where",
    "say goodbye to",
    "are you tired of",
    "tired of",
    "have you ever",
    "did you know",
    "struggling with",
    "looking for",
    "what if i told you",
    "stop scrolling",
)
HOOK_MAX_WORDS = 12
SCRIPT_SYSTEM = (
    "You write the script for a short vertical video ad from a template. "
    "Three parts: a hook spoken or shown in the first two seconds, a body, and a call to "
    "action. Keep the template's shape and beats; make the words specific to the product.\n"
    "Rules:\n"
    "- Write in the language asked for, and nothing else.\n"
    "- The hook is 12 words or fewer and uses one device: a moment the viewer recognises, "
    "who it is for, the product already doing the thing, or one fact from the brief. "
    "It must work with the sound off and say what the first shot shows.\n"
    "- Never open with Introducing, Meet, Imagine, Picture this, In a world where, "
    "Say goodbye to, Are you tired of, Have you ever, Did you know, Struggling with, "
    "Looking for, What if I told you or Stop scrolling, nor with a greeting.\n"
    "- One promise: the body pays off the hook with one demonstration or one true fact, "
    "and says what it does for the viewer. Name the product by the body.\n"
    "- Never invent a fact, a number, a price, a rating, a review or a claim the brief "
    "does not give. A generated person is never a customer.\n"
    "- The person on screen shows and explains; never give them a personal experience "
    "or result (it saved me, I was surprised, it gets me through).\n"
    "- No urgency or scarcity (this week only, last chance, limited) unless the brief "
    "gives a real offer and its end date.\n"
    "- Short spoken sentences; the whole script must be readable in the seconds given.\n"
    "- The call to action is one plain step the viewer can take.\n"
    "- No hashtags, no emoji, no links.\n"
    "- When the brief names an angle and a hook style, the script argues that angle and the "
    "hook works in that style. HOOK2 and HOOK3 are two more hooks for the same body, each in "
    "one of the other styles the brief names, following the same hook rules.\n"
    "Reply with these lines and nothing else:\n"
    "HOOK: …\nBODY: …\nCTA: …\nHOOK2: …\nHOOK3: …"
)
_SCRIPT_LINE = re.compile(r"^\s*(HOOK3|HOOK2|HOOK|BODY|CTA)\s*:\s*(.+?)\s*$", re.I | re.M)
#: Brief lines from the product's own record the script writer may use: its
#: facts and the claims proven on file — never its hashtag or format rules.
_BRIEF_PREFIXES = (
    "In one line",
    "Key features",
    "Audience",
    "Work in these words",
    "Never use these words",
    "Mention the price",
    "Claims you may make",
    "Make no claim",
)


def _hook_ok(hook: str) -> bool:
    low = hook.strip().lower()
    return bool(low) and len(low.split()) <= HOOK_MAX_WORDS and not low.startswith(DEAD_OPENERS)


def _script_from(body: str, brief: str) -> dict[str, Any] | None:
    """The three parts and the alternative hooks that pass (`alts`), or `None`
    when the reply is not the lines asked for, fails the rephrase checks (a
    number the brief never gave, a link), or the main hook breaks the hook
    rules (too long, a dead opener). An alternative that breaks them is dropped."""
    from ai.services import rephrase

    found = {key.lower(): text for key, text in _SCRIPT_LINE.findall(body)}
    parts = {key: found.get(key, "")[:SCRIPT_MAX] for key in ("hook", "body", "cta")}
    if not all(parts.values()) or not rephrase.acceptable(" ".join(parts.values()), brief):
        return None
    if not _hook_ok(parts["hook"]):
        return None
    alts = [
        (index, found[key][:SCRIPT_MAX])
        for index, key in enumerate(("hook2", "hook3"))
        if found.get(key) and _hook_ok(found[key]) and rephrase.adds_nothing(found[key], brief)
    ]
    return {**parts, "alts": alts}


def _brief_row(kind: str, key: str) -> CreativeOption | None:
    """An angle or hook style named in a request: the row, None for blank, 400
    for a key the live catalogue does not hold (rule 18)."""
    if not key:
        return None
    row = CreativeOption.objects.filter(kind=kind, key=key, is_active=True).first()
    if row is None:
        raise ValidationError({kind: f"'{key}' is not in the catalogue."})
    return row


def write_script(
    workspace: Workspace,
    *,
    key: str,
    product: Product,
    language: str,
    seconds: int = 0,
    angle: str = "",
    family: str = "",
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
    angle_row = _brief_row(CreativeKind.ANGLE, angle)
    family_row = _brief_row(CreativeKind.HOOK_FAMILY, family)
    # The main style, then two others for HOOK2 and HOOK3: the angle's own
    # styles first, so "Make 3" tries three ideas rather than three synonyms.
    suited = list((angle_row.metadata or {}).get("families") or []) if angle_row else []
    others = [k for k in suited if k != family][:2]
    other_rows = list(
        CreativeOption.objects.filter(kind=CreativeKind.HOOK_FAMILY, key__in=others, is_active=True)
    )
    other_rows.sort(key=lambda row: others.index(row.key))
    price = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.RECIPE)
    entitlements = entitlements_for(workspace)
    entitlements.require_credits(price)

    from products import brief as product_brief

    base = script_parts(row, None, product)
    meta = row.metadata
    facts = [
        line for line in product_brief.prompt_lines(product) if line.startswith(_BRIEF_PREFIXES)
    ]
    brief = "\n".join(
        line
        for line in (
            f"Product: {product.name}",
            *facts,
            # S17-34: who buys it and what makes them hesitate, in their words.
            f"Who buys it, and when: {product.buyer_situation}" if product.buyer_situation else "",
            f"What makes buyers hesitate: {product.top_objection}" if product.top_objection else "",
            f"Angle: {angle_row.label} — {angle_row.prompt_fragment}" if angle_row else "",
            f"Hook style: {family_row.label} — {family_row.prompt_fragment}" if family_row else "",
            *(
                f"Other hook style for HOOK{index + 2}: {other.label} — {other.prompt_fragment}"
                for index, other in enumerate(other_rows)
            ),
            f"Template: {row.label} — beats: {' · '.join(meta['beats'])}",
            f"Seconds: {seconds}" if seconds else "",
            f"Language: {LANGUAGE_NAMES.get(language or 'en', 'English')}",
            f"Starting hook: {base['hook']}",
            f"Starting body: {base['body']}",
            f"Starting call to action: {base['cta']}",
            # S17-33: what this format's words must be (a value prop's three
            # benefits, a myth and its fact, a chat's question and reply).
            f"This format: {meta['script_hint']}" if meta.get("script_hint") else "",
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
    # S17-36: words the ad may not say on its own — a risky phrase, or a claim
    # with no proof on file — send the template's own words back, free.
    if parts is not None:
        text = " ".join((parts["hook"], parts["body"], parts["cta"]))
        if claims.risky(text) or claims.unproven(text, product):
            parts = None
        else:
            parts["alts"] = [
                (index, alt)
                for index, alt in parts["alts"]
                if not claims.risky(alt) and not claims.unproven(alt, product)
            ]
    if parts is None:
        return {**base, "hooks": [base["hook"]], "families": [], "by": "template", "credits": 0}
    with transaction.atomic():
        ledger.debit_credits(
            workspace,
            price,
            quota=entitlements.quota("monthly_ai_credits"),
            note=f"ad script {row.key}",
        )
    alts = parts.pop("alts")
    hooks = [parts["hook"], *(text for _index, text in alts)]
    families = (
        [family, *(other_rows[index].key for index, _text in alts if index < len(other_rows))]
        if family
        else []
    )
    return {**parts, "hooks": hooks, "families": families, "by": "model", "credits": price}
