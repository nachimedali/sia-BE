"""Replaces the first-generation creative catalog with version 2.

The first seed was written to get the Studio's flow working; version 2
(`ai/creative_seed.py`) is the production catalog — photographic direction,
fidelity and realism rules, and far wider choice. This migration:

1. **Deletes the first-generation rows** — exactly the `(kind, key)` pairs that
   seed shipped, frozen below — unless a row is already a v2 row
   (`metadata.seed == "v2"`). Rows an operator added in admin are never
   touched, and neither is a v2 row an operator has since edited.
2. **Creates every v2 row** with `get_or_create`, so a second run adds nothing
   and changes nothing.
3. **Moves products onto the new keys.** A product's scenes, lights, people
   and calls to action that had a v2 equivalent are re-pointed to it; any key
   the catalog no longer holds is dropped, because the brief validator would
   otherwise refuse the product the next time it is saved.
4. **Re-points past generations** to the same choices under their v2 keys, so
   a post made before this migration can still be regenerated (regeneration
   re-validates the original choices against the live catalog). Nothing is
   dropped from a generation: it still records what was asked.

Off under test settings, like every seed (`common.seeding`). Pre-launch, so
deleting rows here is safe; after launch a catalog change is new rows plus
retirement (`is_active=False`), never deletion.
"""

from django.db import migrations

from ai.creative_seed import CATALOG, SEED_VERSION
from common.seeding import seeding_enabled

#: The first-generation seed, frozen: what this migration may delete.
V1_KEYS: dict[str, tuple[str, ...]] = {
    "scene": ("sidibou", "souk", "studio", "djerba", "dunes", "rooftop"),
    "light": ("dawn", "golden", "noon", "blue"),
    "camera": ("eye", "top", "low", "macro"),
    "cast": ("none", "hands", "one", "group"),
    "vibe": ("casual", "elegant", "family", "athletic"),
    "mood": ("editorial", "minimal", "warm", "vibrant", "cinematic", "film"),
    "palette": ("brand", "sea", "sunset", "mono"),
    "language": ("fr", "en"),
    "cta": ("shop", "learn", "visit", "dm"),
    "format": ("single", "square", "story", "wide", "text"),
    "tempo": ("adagio", "andante", "allegro", "presto"),
    "dynamics": ("pianissimo", "piano", "mezzo-forte", "forte", "fortissimo"),
    "tone": ("playful", "warm", "formal"),
    "toggle": ("headline_on_image", "hashtags", "logo_mark"),
    "preset": ("summer-launch", "ramadan-nights", "evergreen", "souk-story"),
    "quick_tag": ("launch", "seasonal", "behind-the-scenes", "gift-idea", "ramadan"),
    "revise_reason": (
        "wrong-scene",
        "product-too-small",
        "off-brand-colours",
        "caption-tone",
        "text-on-image",
        "more-variety",
    ),
    "audience": (
        "home-cooks",
        "young-professionals",
        "families",
        "tourists",
        "gift-buyers",
        "diaspora",
        "b2b-buyers",
    ),
    "tone_preset": ("artisan", "premium", "playful", "heritage"),
    "claim": ("organic", "handmade", "natural", "made-in-tunisia", "award-winning"),
    "shot_tag": ("front", "side-back", "detail", "packaging", "in-use", "lifestyle"),
    "aspect": ("1-1", "4-5", "9-16", "16-9"),
    "suggestion": (
        "features-handmade",
        "features-small-batch",
        "features-locally-sourced",
        "features-gift-ready",
        "features-sustainable",
        "use_words-made-in-tunisia",
        "use_words-craft",
        "use_words-new-season",
        "use_words-our-story",
        "use_words-small-batch",
        "avoid_words-cheap",
        "avoid_words-best-ever",
        "avoid_words-miracle",
        "avoid_words-guaranteed",
        "avoid_words-limited-time",
        "must_include-logo",
        "must_include-focus",
        "must_include-origin",
        "must_include-cta",
        "restrictions-alcohol",
        "restrictions-competitors",
        "restrictions-health-claims",
        "restrictions-politics",
        "restrictions-pork",
        "restrictions-smoking",
    ),
}

#: First-generation key -> its v2 equivalent, per kind. A key absent here kept
#: its name in v2 (or has no equivalent and is dropped from products).
RENAMED: dict[str, dict[str, str]] = {
    "scene": {
        "sidibou": "sidi-bou-said",
        "souk": "medina-souk",
        "studio": "seamless-studio",
        "djerba": "beach-shoreline",
        "dunes": "desert-dunes",
        "rooftop": "tunis-rooftop",
    },
    "light": {
        "dawn": "soft-daylight",
        "golden": "golden-hour",
        "noon": "hard-sun",
        "blue": "blue-hour",
    },
    "camera": {"eye": "eye-level", "top": "flat-lay", "low": "low-hero"},
    "cast": {"none": "product-only", "one": "model"},
    "vibe": {"casual": "everyday", "elegant": "luxe", "athletic": "active"},
    "mood": {"minimal": "clean"},
    "palette": {"sea": "mediterranean", "sunset": "terracotta", "mono": "monochrome"},
    "cta": {"shop": "shop-now", "learn": "discover", "visit": "visit-store", "dm": "dm-order"},
    "format": {
        "single": "feed-portrait",
        "square": "feed-square",
        "story": "vertical",
        "wide": "landscape",
        "text": "text-only",
    },
    "tone": {"formal": "refined"},
}

#: Generation.creative key -> the kind it names. Every first-generation Studio
#: choice has a v2 equivalent of the same meaning, so a past generation is
#: re-pointed rather than altered: it still records what was asked, and it can
#: still be regenerated, which re-validates its choices against the catalog.
GENERATION_SINGLES = (
    "scene",
    "light",
    "camera",
    "cast",
    "vibe",
    "palette",
    "language",
    "cta",
    "format",
    "tempo",
    "dynamics",
    "tone",
)

#: Product field -> the kind its keys belong to. `people` and `tone_preset`
#: hold one key; the rest hold lists.
PRODUCT_LISTS = {
    "scenes": "scene",
    "lights": "light",
    "aspects": "aspect",
    "audience": "audience",
    "languages": "language",
    "ctas": "cta",
}
PRODUCT_SINGLES = {"people": "cast", "tone_preset": "tone_preset"}


def _replace_catalog(option_model) -> None:
    for kind, keys in V1_KEYS.items():
        stale = option_model.objects.filter(kind=kind, key__in=keys)
        for row in stale:
            if (row.metadata or {}).get("seed") != SEED_VERSION:
                row.delete()
    for kind, rows in CATALOG.items():
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)


def _move_products(option_model, product_model) -> None:
    valid = {
        kind: set(option_model.objects.filter(kind=kind).values_list("key", flat=True))
        for kind in {*PRODUCT_LISTS.values(), *PRODUCT_SINGLES.values(), "claim", "shot_tag"}
    }

    def moved(kind: str, key: str) -> str | None:
        key = RENAMED.get(kind, {}).get(key, key)
        return key if key in valid[kind] else None

    for product in product_model.objects.all().iterator():
        changed: list[str] = []
        for field, kind in PRODUCT_LISTS.items():
            before = list(getattr(product, field) or [])
            after = list(dict.fromkeys(k for k in (moved(kind, str(v)) for v in before) if k))
            if after != before:
                setattr(product, field, after)
                changed.append(field)
        for field, kind in PRODUCT_SINGLES.items():
            before = getattr(product, field) or ""
            after = (moved(kind, before) or "") if before else ""
            if after != before:
                setattr(product, field, after)
                changed.append(field)
        claims = [c for c in product.claims or [] if isinstance(c, dict) and c.get("key") in valid["claim"]]
        if claims != (product.claims or []):
            product.claims = claims
            changed.append("claims")
        tags = {m: t for m, t in (product.reference_tags or {}).items() if t in valid["shot_tag"]}
        if tags != (product.reference_tags or {}):
            product.reference_tags = tags
            changed.append("reference_tags")
        if changed:
            product.save(update_fields=changed)


def _move_generations(generation_model) -> None:
    for generation in generation_model.objects.exclude(creative={}).iterator():
        creative = dict(generation.creative or {})
        for kind in GENERATION_SINGLES:
            value = creative.get(kind)
            if isinstance(value, str):
                creative[kind] = RENAMED.get(kind, {}).get(value, value)
        moods = creative.get("moods")
        if isinstance(moods, list):
            creative["moods"] = list(dict.fromkeys(RENAMED["mood"].get(m, m) for m in moods))
        if creative != generation.creative:
            generation.creative = creative
            generation.save(update_fields=["creative"])


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    _replace_catalog(option_model)
    _move_products(option_model, apps.get_model("products", "Product"))
    _move_generations(apps.get_model("ai", "Generation"))


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0010_claim_phrases"),
        ("products", "0004_product_brief"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
