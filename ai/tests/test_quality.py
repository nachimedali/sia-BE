"""The quality gate (design.md §8.3, I2)."""

from __future__ import annotations

import io

import pytest
from django.test import override_settings
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


def test_legibility_is_absent_rather_than_always_passing() -> None:
    """C-11 / P0-04. The stub used to report `passed: True` for a check it
    never performed, which implies coverage that does not exist — worse than
    no check, because a reader of `checks` cannot tell the difference.

    This asserts the *absence*, so reinstating the key without an OCR port
    behind it fails the build rather than quietly restoring the lie.
    """
    result = run_image_quality_gate(
        content=_png(),
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert "text_legibility" not in result.checks


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


def test_the_repurpose_ceiling_is_wired_in_before_repurpose_generations_are() -> None:
    """`origin_images` has no production caller yet — `GenerationMode.REPURPOSE`
    is still excluded from `pipeline.ALLOWED_MODES`, and §8.9's reissue
    generation is Phase 12/14's.

    A parameter reachable only from tests is the shape A92 and A118 rule
    against, so this is the guard that stops it being a silent promise: the day
    REPURPOSE becomes generatable, this test fails until the pipeline threads
    the origin's bytes through. A silently-unenforced ceiling would be worse
    than an absent one (A128's logic).
    """
    from ai.models import GenerationMode
    from ai.services import pipeline

    assert GenerationMode.REPURPOSE not in pipeline.ALLOWED_MODES, (
        "REPURPOSE is now generatable — pass `origin_images` from the pipeline "
        "into run_image_quality_gate so REPURPOSE_MAX_SIMILARITY is enforced, "
        "then delete this test."
    )


# -----------------------------------------------------------------------------
# The video port — C-11 / P0-04
# -----------------------------------------------------------------------------
def test_a_fresh_checkout_resolves_a_video_provider() -> None:
    """Part 7 rule 6: every external dependency is a port with a fake, and a
    fresh checkout runs end to end with zero third-party accounts."""
    from ai.providers.video import get_video_provider

    provider = get_video_provider()

    assert provider is not None
    result = provider.generate(
        prompt="a mug on a table", reference_images=[], aspect="9:16", duration_seconds=6.0
    )
    assert result.mime == "video/mp4"
    assert result.provider == "fake"


@override_settings(USE_FAKE_AI_PROVIDERS=False, VIDEO_PROVIDER_API_KEY="")
def test_an_unconfigured_deployment_has_no_video_provider() -> None:
    """`None`, not a fake. C-11's complaint was a gate in front of nothing; the
    fix is a port that admits when it is empty rather than one that quietly
    produces a clip nobody rendered."""
    from ai.providers.video import get_video_provider

    assert get_video_provider() is None
