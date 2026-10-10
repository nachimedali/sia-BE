"""The completeness scorer (implementation.md Phase 5.3, reworked for the product
brief — `new_temp/product-new.html`).

Eleven weighted checks summing to 100. `references` alone decides
`is_generation_ready` (I7): the rest are quality-of-generation signal, which is
why a 0%-complete product with one reference image is still generation-ready.
`required` marks what the form needs before it offers "Stage for generation";
it is a *form* gate and never a server refusal — a half-filled product is saved
as a draft, not rejected.

The definitions are data and are served (`GET /products/completeness-checks/`)
so the browser's live panel reads its weights from here rather than carrying a
second copy that would drift. The predicates are not shared: the form evaluates
its own unsaved state, and the number it shows is replaced by this one the
moment the product is saved.

Recomputed by an explicit service call from every mutation path
(`products.services.products`), not a Django signal (Part 7 rule 8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from products.models import Product

#: A photo shot type tag per reference image is what "tagged" means.
TAGGED_PHOTOS_MIN = 3
SHORT_DESCRIPTION_MIN = 40
FEATURES_MIN = 2


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    weight: int
    required: bool
    section: str
    predicate: Callable[[Product], bool]


def _has_references(product: Product) -> bool:
    return product.reference_images.exists()


def _photos_tagged(product: Product) -> bool:
    ids = {str(pk) for pk in product.reference_images.values_list("pk", flat=True)}
    tagged = [k for k in product.reference_tags if k in ids]
    return len(ids) >= TAGGED_PHOTOS_MIN and len(tagged) >= TAGGED_PHOTOS_MIN


def _has_name(product: Product) -> bool:
    return len(product.name.strip()) >= 3


def _has_category(product: Product) -> bool:
    return product.category_id is not None


def _has_short_description(product: Product) -> bool:
    return len(product.short_description.strip()) >= SHORT_DESCRIPTION_MIN


def _has_features(product: Product) -> bool:
    return len(product.features) >= FEATURES_MIN


def _has_tone(product: Product) -> bool:
    """Sliders, a preset, or the older free-text voice descriptor the product
    page still edits — all three say "this product has a voice"."""
    return bool(product.tone) or bool(product.tone_preset) or bool(product.voice.strip())


def _has_words(product: Product) -> bool:
    return bool(product.use_words or product.avoid_words)


def _claims_proven(product: Product) -> bool:
    return all(claim.get("proof_media") for claim in product.claims)


def _rules_reviewed(product: Product) -> bool:
    return product.rules_reviewed_at is not None


def _has_platform(product: Product) -> bool:
    return len(product.platforms) > 0


# Weights sum to 100; `references` carries the most because it is also the I7 gate.
CHECKS: tuple[Check, ...] = (
    Check("references", "At least 1 photo", 18, True, "photos", _has_references),
    Check("photos_tagged", "3 or more photos, tagged", 8, False, "photos", _photos_tagged),
    Check("name", "Product name", 14, True, "identity", _has_name),
    Check("category", "Category", 6, True, "identity", _has_category),
    Check(
        "short_description",
        "Short description, 40+ characters",
        12,
        True,
        "identity",
        _has_short_description,
    ),
    Check("features", "Key features", 5, False, "identity", _has_features),
    Check("tone", "Tone of voice set", 6, False, "voice", _has_tone),
    Check("words", "Words to use or avoid", 4, False, "voice", _has_words),
    Check("claims", "Claims backed by proof", 5, False, "rules", _claims_proven),
    Check("rules_reviewed", "Rules reviewed", 10, True, "rules", _rules_reviewed),
    Check("platforms", "At least 1 platform", 12, True, "stages", _has_platform),
)


def check_definitions() -> list[dict[str, object]]:
    """What the form's panel renders its weights and required flags from."""
    return [
        {
            "key": c.key,
            "text": c.label,
            "weight": c.weight,
            "is_required": c.required,
            "section": c.section,
        }
        for c in CHECKS
    ]


def score_product(product: Product) -> tuple[int, list[dict[str, object]]]:
    """Returns `(completeness_score, missing)`. `missing` lists every failed
    check with its weight as the impact estimate — what the "Complete your
    product" prompts in the template render from."""
    score = 0
    missing: list[dict[str, object]] = []
    for check in CHECKS:
        if check.predicate(product):
            score += check.weight
        else:
            missing.append({"key": check.key, "message": check.label, "impact": check.weight})
    return score, missing


def recompute_completeness(product: Product) -> Product:
    """Persists `completeness_score` and `is_generation_ready`. Call after
    every mutation that could affect either: field edits, and reference image
    attach/detach (products.services.products)."""
    score, _missing = score_product(product)
    product.completeness_score = score
    product.is_generation_ready = product.reference_images.exists()
    product.save(update_fields=["completeness_score", "is_generation_ready", "updated_at"])
    return product


def completeness_payload(product: Product) -> dict[str, object]:
    """What `GET /products/{id}/completeness/` returns."""
    score, missing = score_product(product)
    return {
        "completeness_score": score,
        "is_generation_ready": product.is_generation_ready,
        "missing": missing,
    }
