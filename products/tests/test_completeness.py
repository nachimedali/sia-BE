"""The completeness scorer and the I7 generation-ready flag
(design.md §6.4, implementation.md Phase 5.3; checks reworked for the product
brief)."""

from __future__ import annotations

from typing import Any

import pytest

from categories.models import Category
from products.services.completeness import CHECKS, check_definitions, score_product
from products.services.products import (
    attach_reference_images,
    detach_reference_image,
    update_product,
)

pytestmark = pytest.mark.django_db


def test_weights_sum_to_one_hundred() -> None:
    assert sum(c.weight for c in CHECKS) == 100
    assert {d["key"] for d in check_definitions()} == {c.key for c in CHECKS}


def test_is_generation_ready_flips_on_first_and_last_reference_image(
    product: Any, make_png_upload: Any
) -> None:
    before_first: bool = product.is_generation_ready
    assert before_first is False

    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    after_first: bool = product.is_generation_ready
    assert after_first is True

    last_image = product.reference_images.get()
    detach_reference_image(product=product, media_asset=last_image)
    product.refresh_from_db()
    after_last_removed: bool = product.is_generation_ready
    assert after_last_removed is False


def test_generation_readiness_ignores_every_other_check(product: Any, make_png_upload: Any) -> None:
    """I7: one photo is the whole gate. The rules-reviewed and platform checks
    that the *form* requires before staging never become a server refusal."""
    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    assert product.is_generation_ready is True
    assert product.completeness_score < 100


def test_completeness_score_monotonic_as_the_brief_is_filled(
    product: Any, make_png_upload: Any
) -> None:
    category = Category.objects.create(name="Ceramics", slug="ceramics")
    scores = [score_product(product)[0]]

    def step(**fields: Any) -> None:
        update_product(product, **fields)
        scores.append(score_product(product)[0])

    step(category=category)
    step(short_description="Hand-glazed 12oz mug, matte finish, six colourways.")
    step(features=["Hand-made", "Small batch"])
    step(tone={"formal": 40, "bold": 45, "modern": 50, "poetic": 50})
    step(use_words=["craft"])
    step(rules_reviewed=True)
    step(platforms=["instagram"])
    attach_reference_images(
        product=product, uploads=[make_png_upload()] * 3, shot_tags=["front", "side-back", "detail"]
    )
    product.refresh_from_db()
    scores.append(score_product(product)[0])

    assert scores == sorted(scores)
    assert scores[0] < scores[-1]
    assert scores[-1] == 100


def test_missing_lists_every_unsatisfied_check_with_a_positive_impact(product: Any) -> None:
    _score, missing = score_product(product)
    keys = {item["key"] for item in missing}
    assert {"references", "category", "short_description", "rules_reviewed", "platforms"} <= keys
    assert all(int(item["impact"]) > 0 for item in missing)  # type: ignore[call-overload]


def test_an_unproven_claim_keeps_the_claims_check_open(product: Any) -> None:
    update_product(product, claims=[{"key": "organic", "proof_media": None}])
    assert "claims" in {item["key"] for item in score_product(product)[1]}


def test_tagging_needs_three_photos_each_tagged(product: Any, make_png_upload: Any) -> None:
    attach_reference_images(
        product=product, uploads=[make_png_upload()] * 2, shot_tags=["front", "detail"]
    )
    assert "photos_tagged" in {item["key"] for item in score_product(product)[1]}
    attach_reference_images(product=product, uploads=[make_png_upload()], shot_tags=["packaging"])
    assert "photos_tagged" not in {item["key"] for item in score_product(product)[1]}


def test_the_older_voice_descriptor_still_counts_as_a_tone(product: Any) -> None:
    assert "tone" in {item["key"] for item in score_product(product)[1]}
    update_product(product, voice="Warm, plain-spoken")
    assert "tone" not in {item["key"] for item in score_product(product)[1]}
