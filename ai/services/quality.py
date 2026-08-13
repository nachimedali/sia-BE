"""The quality gate (design.md §8.3). Every generated asset is checked before
the user ever sees it; credits debit only for output that passes (I2).

Four of design.md's five image checks are real, working implementations, not
mocks standing in for a vendor:

* **file integrity** (stand-in for "artifact/defect detection") — Pillow
  decodes and verifies the file. A truncated or corrupt image is a real
  defect this genuinely catches, even though it says nothing about AI
  artifacts in the visual sense.
* **resolution/aspect compliance** — measured directly against what was
  requested.
* **product identity similarity** — a real perceptual hash (dHash) Hamming
  distance against each of the product's reference images, not a fake
  vendor's canned score. Deterministic and provider-agnostic: it scores fake
  and real provider output identically.
* **brand-constraint verification** — delegates to `TextProvider.
  classify_constraints`, the vision-as-labelling call design.md §5 already
  puts on the LLM provider (see `ai/providers/base.py`).

**Text-in-image legibility is the one left undone.** It needs OCR, which
needs a vendor this codebase does not have credentials for or a documented
choice of yet, and it only matters once image generation produces text
overlays — a Phase 14 (`ShotSpec` overlays) concern. The check exists and
always passes, with that reason recorded on every `QualityCheck` row rather
than silently omitted (design.md §15.8 A71).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PIL import Image

from common.media import probe_image_integrity

if TYPE_CHECKING:
    from ai.providers.base import TextProvider

_ASPECT_RATIOS: dict[str, float] = {"1:1": 1.0, "4:5": 4 / 5, "16:9": 16 / 9, "9:16": 9 / 16}
_ASPECT_TOLERANCE = 0.08
_HASH_SIZE = 8


@dataclass(frozen=True)
class QualityResult:
    passed: bool
    checks: dict[str, Any]
    identity_score: float | None
    rejected_reason: str = ""


# -----------------------------------------------------------------------------
# Perceptual hash — a real algorithm (dHash), not a mocked similarity score.
# -----------------------------------------------------------------------------
def _dhash(image: Image.Image, hash_size: int = _HASH_SIZE) -> int:
    resized = image.convert("L").resize((hash_size + 1, hash_size))
    # Pillow's stub types this broadly to cover every image mode; a
    # single-channel "L" image always yields a plain int per pixel at runtime.
    pixels = [int(value) for value in resized.get_flattened_data()]  # type: ignore[arg-type]
    bits = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits = (bits << 1) | int(pixels[offset + col] > pixels[offset + col + 1])
    return bits


def _similarity_from_hash(
    candidate_hash: int, reference: bytes, *, hash_size: int = _HASH_SIZE
) -> float:
    with Image.open(io.BytesIO(reference)) as ref_img:
        reference_hash = _dhash(ref_img, hash_size)
    max_bits = hash_size * hash_size
    distance = bin(candidate_hash ^ reference_hash).count("1")
    return 1 - (distance / max_bits)


# -----------------------------------------------------------------------------
# Individual checks
# -----------------------------------------------------------------------------
def _check_file_integrity(content: bytes) -> tuple[bool, str]:
    return probe_image_integrity(io.BytesIO(content))


def _check_aspect(width: int, height: int, requested_aspect: str) -> tuple[bool, str]:
    expected = _ASPECT_RATIOS.get(requested_aspect)
    if expected is None or height == 0:
        # An aspect string the checker does not recognise is not evidence of
        # a defect — it fails open rather than rejecting on an unknown label.
        return True, ""
    actual = width / height
    if abs(actual - expected) / expected > _ASPECT_TOLERANCE:
        return False, (
            f"{width}x{height} (ratio {actual:.2f}) does not match "
            f"requested {requested_aspect} (ratio {expected:.2f})"
        )
    return True, ""


# -----------------------------------------------------------------------------
# Gates
# -----------------------------------------------------------------------------
#: A repurposed post has to be recognisably the same idea but not the same
#: asset (design.md §8.9: "capped at 0.8 similarity to the original"). The
#: ceiling lives here rather than in `analytics.services.repurposing` because
#: this is where every other similarity judgement is made — a second
#: implementation would be a second opinion about what "similar" means.
REPURPOSE_MAX_SIMILARITY = 0.8


def run_image_quality_gate(
    *,
    content: bytes,
    requested_aspect: str,
    reference_images: list[bytes],
    restrictions: list[str],
    text_provider: TextProvider,
    identity_similarity_threshold: float,
    origin_images: list[bytes] | None = None,
) -> QualityResult:
    """`origin_images` is the post being repurposed, when there is one.

    It turns the identity check's floor into a *band*: still similar enough to
    the product's references, but no longer than `REPURPOSE_MAX_SIMILARITY` to
    the original post's own asset. Absent for every other generation mode, where
    there is no original to be too close to.
    """
    checks: dict[str, Any] = {}
    reasons: list[str] = []

    integrity_ok, integrity_detail = _check_file_integrity(content)
    checks["file_integrity"] = {"passed": integrity_ok, "detail": integrity_detail}
    if not integrity_ok:
        # Nothing else is checkable against a file that will not decode.
        return QualityResult(
            passed=False,
            checks=checks,
            identity_score=None,
            rejected_reason=f"file_integrity: {integrity_detail}",
        )

    with Image.open(io.BytesIO(content)) as img:
        width, height = img.size
        candidate_hash = _dhash(img) if (reference_images or origin_images) else None

    aspect_ok, aspect_detail = _check_aspect(width, height, requested_aspect)
    checks["resolution_and_aspect"] = {
        "passed": aspect_ok,
        "detail": aspect_detail,
        "width": width,
        "height": height,
    }
    if not aspect_ok:
        reasons.append(f"resolution_and_aspect: {aspect_detail}")

    identity_score: float | None = None
    if reference_images and candidate_hash is not None:
        identity_score = max(
            _similarity_from_hash(candidate_hash, ref) for ref in reference_images
        )
        identity_ok = identity_score >= identity_similarity_threshold
        checks["product_identity"] = {
            "passed": identity_ok,
            "score": identity_score,
            "threshold": identity_similarity_threshold,
        }
        if not identity_ok:
            reasons.append(
                f"product_identity: score {identity_score:.2f} below "
                f"threshold {identity_similarity_threshold:.2f}"
            )

    if origin_images and candidate_hash is not None:
        origin_score = max(
            _similarity_from_hash(candidate_hash, origin) for origin in origin_images
        )
        origin_ok = origin_score <= REPURPOSE_MAX_SIMILARITY
        checks["repurpose_distance"] = {
            "passed": origin_ok,
            "score": origin_score,
            "ceiling": REPURPOSE_MAX_SIMILARITY,
        }
        if not origin_ok:
            reasons.append(
                f"repurpose_distance: score {origin_score:.2f} above "
                f"ceiling {REPURPOSE_MAX_SIMILARITY:.2f} — too close to the original"
            )

    violated: list[str] = []
    if restrictions:
        violated = text_provider.classify_constraints(
            image_bytes=content, restrictions=restrictions
        )
        checks["brand_constraints"] = {"passed": not violated, "violated": violated}
        if violated:
            reasons.append(f"brand_constraints: violates {violated}")

    checks["text_legibility"] = {
        "passed": True,
        "detail": "not yet implemented — deferred to Phase 14 overlays (A71)",
    }

    return QualityResult(
        passed=not reasons,
        checks=checks,
        identity_score=identity_score,
        rejected_reason="; ".join(reasons),
    )


def run_text_quality_gate(*, body: str, banned_phrases: list[str]) -> QualityResult:
    checks: dict[str, Any] = {}
    reasons: list[str] = []

    non_empty = bool(body.strip())
    checks["non_empty"] = {"passed": non_empty}
    if not non_empty:
        reasons.append("non_empty: body is blank")

    hits = [phrase for phrase in banned_phrases if phrase.lower() in body.lower()]
    checks["banned_phrases"] = {"passed": not hits, "hits": hits}
    if hits:
        reasons.append(f"banned_phrases: contains {hits}")

    return QualityResult(
        passed=not reasons, checks=checks, identity_score=None, rejected_reason="; ".join(reasons)
    )
