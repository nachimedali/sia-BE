"""The quality gate (design.md §8.3, I2)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from ai.providers.fake import FORCE_LOW_SIMILARITY_SENTINEL, FakeTextProvider
from ai.services.quality import run_image_quality_gate, run_text_quality_gate


def _png(size: tuple[int, int] = (256, 256), color: tuple[int, int, int] = (10, 200, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_identical_image_scores_perfect_identity_similarity() -> None:
    reference = _png()

    result = run_image_quality_gate(
        content=reference,
        requested_aspect="1:1",
        reference_images=[reference],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert result.passed
    assert result.identity_score == pytest.approx(1.0)


def test_unrelated_image_fails_identity_similarity() -> None:
    from ai.providers.fake import _render

    reference = _png(color=(10, 200, 30))
    unrelated = _render(f"anything {FORCE_LOW_SIMILARITY_SENTINEL}", [], 256, 256, variant_index=0)

    result = run_image_quality_gate(
        content=unrelated,
        requested_aspect="1:1",
        reference_images=[reference],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert not result.passed
    assert "product_identity" in result.rejected_reason


def test_corrupt_file_fails_integrity_before_anything_else() -> None:
    result = run_image_quality_gate(
        content=b"not a real image",
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert not result.passed
    assert "file_integrity" in result.rejected_reason
    # Nothing downstream ran against an undecodable file.
    assert "resolution_and_aspect" not in result.checks


def test_aspect_mismatch_is_rejected() -> None:
    result = run_image_quality_gate(
        content=_png(size=(256, 256)),
        requested_aspect="16:9",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert not result.passed
    assert "resolution_and_aspect" in result.rejected_reason


def test_brand_constraint_violation_is_rejected() -> None:
    result = run_image_quality_gate(
        content=_png(),
        requested_aspect="1:1",
        reference_images=[],
        restrictions=["FORCE_VIOLATION: always show the sole"],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert not result.passed
    assert "brand_constraints" in result.rejected_reason


def test_no_restrictions_skips_the_brand_constraint_check() -> None:
    result = run_image_quality_gate(
        content=_png(),
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert "brand_constraints" not in result.checks


def test_text_legibility_always_passes_and_says_why() -> None:
    result = run_image_quality_gate(
        content=_png(),
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert result.checks["text_legibility"]["passed"] is True
    assert "Phase 14" in result.checks["text_legibility"]["detail"]


def test_text_gate_passes_clean_copy() -> None:
    result = run_text_quality_gate(
        body="A cosy morning with your favourite mug.", banned_phrases=[]
    )
    assert result.passed


def test_text_gate_rejects_blank_output() -> None:
    result = run_text_quality_gate(body="   ", banned_phrases=[])
    assert not result.passed
    assert "non_empty" in result.rejected_reason


def test_text_gate_rejects_banned_phrases() -> None:
    result = run_text_quality_gate(
        body="Let's leverage synergy for growth.", banned_phrases=["synergy"]
    )
    assert not result.passed
    assert "banned_phrases" in result.rejected_reason
