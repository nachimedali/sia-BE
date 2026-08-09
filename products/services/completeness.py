"""The completeness scorer (implementation.md Phase 5.3).

Eight weighted checks, summing to 100. `references` alone decides
`is_generation_ready` (I7) — the other seven are quality-of-generation
signal, not a gate, which is why a 0%-complete product with one reference
image is still generation-ready.

`motion_reference` has no field of its own on `Product`: design.md §6.4 does
not list one, and Phase 14 (video) is where a dedicated motion-reference
concept would earn a column. Read literally in the meantime as "if this
product claims the `video` format, at least one of its reference images
should actually be a video" — reusing `MediaAsset.kind` rather than inventing
new state (design.md §15.6 A54).

Recomputed by an explicit service call from every mutation path
(`products.services.products`), not a Django signal — the rest of this
codebase has no signal wiring, and implementation.md's "signal or explicit
service call" leaves the choice open (design.md §15.6 A54).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from content.models import MediaKind
from products.models import Product, ProductFormat


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    weight: int
    predicate: Callable[[Product], bool]


def _has_references(product: Product) -> bool:
    return product.reference_images.exists()


def _has_description(product: Product) -> bool:
    return len(product.description.strip()) >= 10


def _has_restrictions(product: Product) -> bool:
    return len(product.restrictions) > 0


def _has_voice(product: Product) -> bool:
    return bool(product.voice.strip())


def _has_formats(product: Product) -> bool:
    return len(product.formats) > 0


def _has_platforms(product: Product) -> bool:
    return len(product.platforms) > 0


def _has_ctas(product: Product) -> bool:
    return len(product.ctas) > 0


def _has_motion_reference(product: Product) -> bool:
    if ProductFormat.VIDEO not in product.formats:
        return True  # not applicable — nothing to satisfy
    return product.reference_images.filter(kind=MediaKind.VIDEO).exists()


# Order matches implementation.md Phase 5.3's own listing. Weights sum to 100;
# `references` carries the most because it is also the I7 gate.
CHECKS: tuple[Check, ...] = (
    Check("references", "At least one reference image", 25, _has_references),
    Check("description", "A product description", 15, _has_description),
    Check("restrictions", "At least one hard restriction", 10, _has_restrictions),
    Check("voice", "A voice descriptor", 10, _has_voice),
    Check("formats", "At least one preferred format", 10, _has_formats),
    Check("platforms", "At least one target platform", 10, _has_platforms),
    Check("ctas", "At least one call to action", 10, _has_ctas),
    Check("motion_reference", "A video reference for the video format", 10, _has_motion_reference),
)


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
