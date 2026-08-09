"""The completeness scorer and the I7 generation-ready flag
(design.md §6.4, implementation.md Phase 5.3)."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from content.services.media import ingest_media
from products.services.completeness import score_product
from products.services.products import (
    attach_reference_images,
    detach_reference_image,
    update_product,
)

pytestmark = pytest.mark.django_db


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


def test_completeness_score_monotonic_as_fields_are_filled(
    product: Any, make_png_upload: Any
) -> None:
    scores = [score_product(product)[0]]

    update_product(product, description="Hand-glazed 12oz mug, matte finish.")
    scores.append(score_product(product)[0])

    update_product(product, restrictions=["Never claim dishwasher-safe"])
    scores.append(score_product(product)[0])

    update_product(product, voice="Playful, confident, no corporate jargon")
    scores.append(score_product(product)[0])

    update_product(product, formats=["image"])
    scores.append(score_product(product)[0])

    update_product(product, platforms=["instagram"])
    scores.append(score_product(product)[0])

    update_product(product, ctas=["Shop now"])
    scores.append(score_product(product)[0])

    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    scores.append(score_product(product)[0])

    assert scores == sorted(scores)
    assert scores[0] < scores[-1]
    assert scores[-1] == 100


def test_completeness_missing_lists_every_unsatisfied_check_with_a_positive_impact(
    product: Any,
) -> None:
    _score, missing = score_product(product)
    keys = {item["key"] for item in missing}
    assert "references" in keys
    assert "description" in keys
    assert all(int(item["impact"]) > 0 for item in missing)  # type: ignore[call-overload]


def test_motion_reference_requires_a_video_kind_asset_when_video_is_requested(
    product: Any, make_png_upload: Any
) -> None:
    update_product(product, formats=["video"])
    _score, missing = score_product(product)
    assert "motion_reference" in {item["key"] for item in missing}

    # An image reference satisfies I7's `is_generation_ready`, but not the
    # video-specific completeness check — the two measure different things.
    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    assert product.is_generation_ready is True
    _score, missing = score_product(product)
    assert "motion_reference" in {item["key"] for item in missing}

    video_asset = ingest_media(
        workspace=product.workspace,
        upload=SimpleUploadedFile("clip.mp4", b"fake-mp4-bytes", content_type="video/mp4"),
    )
    product.reference_images.add(video_asset)
    _score, missing = score_product(product)
    assert "motion_reference" not in {item["key"] for item in missing}


def test_motion_reference_is_not_applicable_without_the_video_format(product: Any) -> None:
    _score, missing = score_product(product)
    assert "motion_reference" not in {item["key"] for item in missing}
