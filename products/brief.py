"""The product brief — what `/app/products/new` collects beyond a name (L-1, C-08).

**Overrides, not a second identity.** Voice lives on the workspace's
`TasteProfile`; this is the narrow, per-product layer on top of it. Nothing here
is versioned or becomes the brand: it narrows (words to avoid, things never to
show) or informs (features, audience, tone leaning) one product's generations.

Three jobs, all pure functions over a `Product` so they are tested without a
request:

* `validate` — every key that names a catalog row (`CreativeOption`) is checked
  against the live catalog, so an unknown or retired key is a 400 rather than a
  silently ignored value (the Studio's rule, Part 7 rule 18).
* `prompt_lines` — what the generator is told, appended to the restriction
  block the prompts already carry.
* `constraints` — the brand-policy constraints this product contributes to
  screening, merged by `taste.services.profiles.constraints_for` and so
  **narrowing only**, like `ProductTasteOverride`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from rest_framework.exceptions import ValidationError

from ai.models import CreativeKind, CreativeOption
from content.models import MediaAsset, Platform
from content.services.rules import formats_for
from products.models import HashtagStyle, Product

TONE_KEYS = ("formal", "bold", "modern", "poetic")
PHOTO_POLICY_KEYS = ("bg_remove", "restage", "unaltered", "logo_legible")
LIST_MAX = 15
WORD_MAX = 100
COLORS_MAX = 4
PER_WEEK_MAX = 21

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HASHTAG = re.compile(r"^#\w{1,60}$")

#: The four sliders as words, five stops each — the same steps the form shows
#: beside the slider, so what the user read is what the model is told.
_TONE_WORDS: dict[str, tuple[str, str, str, str, str]] = {
    "formal": ("playful", "friendly", "balanced in register", "polished", "formal"),
    "bold": ("hushed", "calm", "steady", "lively", "bold"),
    "modern": ("heritage-minded", "classic", "timeless", "fresh", "modern"),
    "poetic": ("plain-spoken", "clear", "vivid", "lyrical", "poetic"),
}


def _stop(value: int) -> int:
    return min(4, int(value // 20.01))


def _keys(kind: str) -> set[str]:
    return set(
        CreativeOption.objects.filter(kind=kind, is_active=True).values_list("key", flat=True)
    )


def _text_list(
    name: str, value: Any, errors: dict[str, str], *, pattern: re.Pattern[str] | None = None
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors[name] = "Expected a list of text."
        return []
    items = list(dict.fromkeys(v.strip() for v in value if v.strip()))
    if len(items) > LIST_MAX:
        errors[name] = f"At most {LIST_MAX} entries."
    elif any(len(v) > WORD_MAX for v in items):
        errors[name] = f"Each entry is at most {WORD_MAX} characters."
    elif pattern and not all(pattern.match(v) for v in items):
        errors[name] = "Entries must look like #hashtag."
    return items


def _catalog_list(name: str, value: Any, kind: str, errors: dict[str, str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors[name] = "Expected a list of keys."
        return []
    unknown = [v for v in value if v not in _keys(kind)]
    if unknown:
        errors[name] = f"Not available: {', '.join(unknown)}."
        return []
    return list(dict.fromkeys(value))


def validate(
    data: dict[str, Any], *, workspace_id: int | None, instance: Product | None
) -> dict[str, Any]:
    """Returns `data` with the brief's lists normalised, or raises one
    `ValidationError` carrying every problem (a form fixes them in one pass)."""
    errors: dict[str, str] = {}
    out = dict(data)

    for name in ("features", "use_words", "avoid_words", "must_include"):
        if name in data:
            out[name] = _text_list(name, data[name], errors)
    if "brand_hashtags" in data:
        out["brand_hashtags"] = [
            "#" + v.lstrip("#")
            for v in _text_list("brand_hashtags", data["brand_hashtags"], errors)
            if v.lstrip("#")
        ]
        if not all(_HASHTAG.match(v) for v in out["brand_hashtags"]):
            errors["brand_hashtags"] = "Hashtags are letters, digits and underscores."

    for name, kind in (
        ("audience", CreativeKind.AUDIENCE),
        ("scenes", CreativeKind.SCENE),
        ("lights", CreativeKind.LIGHT),
        ("aspects", CreativeKind.ASPECT),
        ("languages", CreativeKind.LANGUAGE),
    ):
        if name in data:
            out[name] = _catalog_list(name, data[name], kind, errors)

    for name, kind in (("people", CreativeKind.CAST), ("tone_preset", CreativeKind.TONE_PRESET)):
        value = data.get(name)
        if value and value not in _keys(kind):
            errors[name] = f"'{value}' is not available."

    if "tone" in data:
        tone = data["tone"]
        if not isinstance(tone, dict) or set(tone) - set(TONE_KEYS):
            errors["tone"] = f"Expected {', '.join(TONE_KEYS)}, each 0-100."
        elif not all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100 for v in tone.values()
        ):
            errors["tone"] = "Each slider is a whole number from 0 to 100."

    if "brand_colors" in data:
        colors = data["brand_colors"]
        if (
            not isinstance(colors, list)
            or len(colors) > COLORS_MAX
            or not all(isinstance(c, str) and _HEX.match(c) for c in colors)
        ):
            errors["brand_colors"] = f"Up to {COLORS_MAX} colours as #RRGGBB."
        else:
            out["brand_colors"] = [c.upper() for c in colors]

    if "photo_policy" in data:
        policy = data["photo_policy"]
        if (
            not isinstance(policy, dict)
            or set(policy) - set(PHOTO_POLICY_KEYS)
            or not all(isinstance(v, bool) for v in policy.values())
        ):
            errors["photo_policy"] = f"Expected true/false for {', '.join(PHOTO_POLICY_KEYS)}."

    if "claims" in data:
        out["claims"] = _claims(data["claims"], workspace_id, errors)

    if "reference_tags" in data:
        out["reference_tags"] = _reference_tags(data["reference_tags"], instance, errors)

    if "platform_plan" in data or "platforms" in data:
        plan = data.get("platform_plan", instance.platform_plan if instance else {})
        platforms = data.get("platforms", instance.platforms if instance else [])
        _platform_plan(plan, platforms, errors)

    starts = data.get("campaign_starts", instance.campaign_starts if instance else None)
    ends = data.get("campaign_ends", instance.campaign_ends if instance else None)
    if starts and ends and ends < starts:
        errors["campaign_ends"] = "The end date is before the start date."

    if errors:
        raise ValidationError(errors)
    return out


def _claims(value: Any, workspace_id: int | None, errors: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors["claims"] = "Expected a list of {key, proof_media}."
        return []
    known = _keys(CreativeKind.CLAIM)
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"key", "proof_media"}:
            errors["claims"] = "Each claim is {key, proof_media}."
            return []
        key, proof = entry.get("key"), entry.get("proof_media")
        if key not in known:
            errors["claims"] = f"'{key}' is not an available claim."
            return []
        if proof is not None and not (
            isinstance(proof, int)
            and workspace_id is not None
            and MediaAsset.objects.filter(pk=proof, workspace_id=workspace_id).exists()
        ):
            # Another workspace's asset reads as missing, never as forbidden.
            errors["claims"] = f"Proof for '{key}' was not found."
            return []
        if any(o["key"] == key for o in out):
            continue
        out.append({"key": key, "proof_media": proof})
    return out


def _reference_tags(value: Any, instance: Product | None, errors: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        errors["reference_tags"] = "Expected {media id: shot type}."
        return {}
    attached = (
        {str(pk) for pk in instance.reference_images.values_list("pk", flat=True)}
        if instance
        else set()
    )
    known = _keys(CreativeKind.SHOT_TAG)
    if set(value) - attached:
        errors["reference_tags"] = "Only this product's reference photos can be tagged."
    elif not all(v in known for v in value.values()):
        errors["reference_tags"] = "Unknown shot type."
    return dict(value)


def _platform_plan(plan: Any, platforms: Iterable[str], errors: dict[str, str]) -> None:
    if not isinstance(plan, dict):
        errors["platform_plan"] = "Expected {platform: {formats, per_week}}."
        return
    if set(plan) - set(platforms):
        errors["platform_plan"] = "A plan is only kept for a selected platform."
        return
    for platform, entry in plan.items():
        if (
            platform not in Platform.values
            or not isinstance(entry, dict)
            or set(entry) - {"formats", "per_week"}
        ):
            errors["platform_plan"] = f"Bad plan for '{platform}'."
            return
        formats, per_week = entry.get("formats", []), entry.get("per_week", 1)
        allowed = set(formats_for(platform))
        if not isinstance(formats, list) or not formats or set(formats) - allowed:
            errors["platform_plan"] = f"'{platform}' does not offer those formats."
            return
        if (
            not isinstance(per_week, int)
            or isinstance(per_week, bool)
            or not 1 <= per_week <= PER_WEEK_MAX
        ):
            errors["platform_plan"] = f"Posts per week is 1-{PER_WEEK_MAX}."
            return


def proven_claims(product: Product) -> list[str]:
    """Labels of the claims the generator may use: **proof attached only**. A
    claim someone ticked but never backed is not a claim the brand can make."""
    keys = [c["key"] for c in product.claims if c.get("proof_media")]
    labels = dict(
        CreativeOption.objects.filter(kind=CreativeKind.CLAIM, key__in=keys).values_list(
            "key", "label"
        )
    )
    return [labels.get(k, k) for k in keys]


def unproven_claims(product: Product) -> list[str]:
    return [c["key"] for c in product.claims if not c.get("proof_media")]


def _keyed_labels(kind: str, keys: Iterable[str]) -> list[str]:
    labels = dict(
        CreativeOption.objects.filter(kind=kind, key__in=list(keys)).values_list("key", "label")
    )
    return [labels.get(k, k) for k in keys]


def prompt_lines(product: Product) -> list[str]:
    """The brief as instructions. Empty fields say nothing — a prompt padded
    with "Features: none" teaches the model to invent some."""
    lines: list[str] = []
    if product.short_description:
        lines.append(f"In one line: {product.short_description}")
    if product.features:
        lines.append("Key features: " + ", ".join(product.features) + ".")
    if product.audience:
        lines.append(
            "Audience: " + ", ".join(_keyed_labels(CreativeKind.AUDIENCE, product.audience)) + "."
        )
    if product.tone:
        parts = [_TONE_WORDS[k][_stop(v)] for k, v in product.tone.items() if k in _TONE_WORDS]
        lines.append("Lean toward a " + ", ".join(parts) + " voice for this product.")
    if product.use_words:
        lines.append(
            "Work in these words or phrases where natural: " + ", ".join(product.use_words) + "."
        )
    if product.avoid_words:
        lines.append(
            "Never use these words: " + ", ".join(f'"{w}"' for w in product.avoid_words) + "."
        )
    if product.hashtags_style == HashtagStyle.NONE:
        lines.append("Use no hashtags.")
    if product.caption_length:
        lines.append(f"Caption length: {product.get_caption_length_display().lower()}.")
    if product.must_include:
        lines.append("Every post must include: " + ", ".join(product.must_include) + ".")
    if product.brand_hashtags:
        lines.append("Brand hashtags: " + " ".join(product.brand_hashtags) + ".")
    if product.mention_price and product.price is not None:
        lines.append(f"Mention the price: {product.price.normalize():f} {product.price_currency}.")
    if product.no_children:
        lines.append("Show no children, in words or in imagery; adults only.")
    proven = proven_claims(product)
    if proven:
        lines.append("Claims you may make (proof is on file): " + ", ".join(proven) + ".")
    if unproven_claims(product):
        lines.append(
            "Make no claim about certification, origin, materials or awards "
            "beyond those listed above."
        )
    if product.legal_mention:
        lines.append(f"End with this legal mention, verbatim: {product.legal_mention}")
    return lines


def constraints(product: Product) -> dict[str, Any]:
    """This product's contribution to brand-policy screening. Only what a text
    check can honestly decide: banned words, and "no hashtags". The rest
    (`must_include` is largely visual, 'no children' is imagery) is told to the
    model but never claimed as screened."""
    out: dict[str, Any] = {}
    if product.avoid_words:
        out["banned_phrases"] = list(product.avoid_words)
    if product.restrictions:
        out["banned_topics"] = list(product.restrictions)
    if product.hashtags_style == HashtagStyle.NONE:
        out["max_hashtags"] = 0
    return out
