"""Deterministic `TextProvider`/`ImageProvider` for tests and dev (A8).

The image fake draws a real, valid PNG via Pillow rather than returning
opaque bytes — the quality gate's checks (resolution, aspect, file
integrity, perceptual-hash similarity against a product's reference images)
run identically against fake and real output, so a green suite proves the
gate actually works rather than only proving it can be monkeypatched around.

Two sentinels let a test force a specific gate outcome without a real
provider: a restriction containing `FORCE_VIOLATION_SENTINEL` is always
reported as violated by `classify_constraints`; a prompt containing
`FORCE_LOW_SIMILARITY_SENTINEL` makes the generated image ignore the
reference entirely, so identity similarity comes out low on purpose.
"""

from __future__ import annotations

import hashlib
import io
import math
import zlib
from typing import Any

from PIL import Image

from ai.providers.base import (
    ImageGenerationResult,
    ImageVariant,
    TextGenerationResult,
    TextVariant,
)
from common.text import content_tokens

FORCE_VIOLATION_SENTINEL = "FORCE_VIOLATION"
FORCE_LOW_SIMILARITY_SENTINEL = "FORCE_LOW_SIMILARITY"

# design.md §4.3 default; aspect strings the FE composer offers.
_ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (256, 256),
    "4:5": (256, 320),
    "16:9": (320, 180),
    "9:16": (180, 320),
}
_DEFAULT_SIZE = (256, 256)


def _size_for_aspect(aspect: str) -> tuple[int, int]:
    return _ASPECT_SIZES.get(aspect, _DEFAULT_SIZE)


def _gradient(width: int, height: int, seed: int) -> Image.Image:
    """A strictly right-to-left *decreasing* gradient, not a flat fill.

    dHash only encodes the sign of each adjacent-pixel step. A flat image
    gives "equal" (bit 0) at every step; an *increasing* gradient gives
    "not greater" (bit 0 too) — indistinguishable from flat by this hash,
    including against the flat-colour test fixtures this codebase's own
    uploads use. A *decreasing* gradient gives "greater" (bit 1) at every
    step instead, landing on the hash maximally far from flat's — which is
    what makes this reliably score as dissimilar from any real reference.
    """
    image = Image.new("RGB", (width, height))
    base = seed % 156  # leaves headroom to reach 255 without wrapping
    denom = max(1, width - 1)
    for y in range(height):
        for x in range(width):
            value = 255 - base - (x * (255 - base)) // denom
            image.putpixel((x, y), (value, value, value))
    return image


def _render(
    prompt: str, reference_images: list[bytes], width: int, height: int, *, variant_index: int
) -> bytes:
    use_reference = reference_images and FORCE_LOW_SIMILARITY_SENTINEL not in prompt.upper()
    if use_reference:
        base = Image.open(io.BytesIO(reference_images[0])).convert("RGB").resize((width, height))
    else:
        # Reference-independent but still deterministic per prompt, and
        # distinct enough from any real reference to fail the identity check
        # on purpose when nothing is grounding this generation.
        base = _gradient(width, height, zlib.crc32(prompt.encode()))

    # A small per-variant stamp keeps variants distinguishable without moving
    # the perceptual hash enough to flip the identity check — dHash averages
    # over ~1/9th-width cells, and the stamp is a fraction of one cell.
    stamp = max(2, min(width, height) // 25)
    stamp_color = (variant_index * 37 % 256, 0, 0)
    for x in range(stamp):
        for y in range(stamp):
            base.putpixel((x, y), stamp_color)

    buffer = io.BytesIO()
    base.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeTextProvider:
    """Records calls instead of making them (mirrors `FakeBillingGateway`)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(
        self, *, system: str, prompt: str, n: int, model: str | None = None
    ) -> TextGenerationResult:
        self.calls.append({"system": system, "prompt": prompt, "n": n, "model": model})
        variants = [
            TextVariant(
                body=f"{prompt.strip()} — variant {i + 1}", rationale=f"fake rationale {i + 1}"
            )
            for i in range(n)
        ]
        tokens_in = len(system.split()) + len(prompt.split())
        tokens_out = sum(len(v.body.split()) for v in variants)
        return TextGenerationResult(
            variants=variants,
            provider="fake",
            model=model or "fake-text-1",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=5,
        )

    def classify_constraints(self, *, image_bytes: bytes, restrictions: list[str]) -> list[str]:
        return [r for r in restrictions if FORCE_VIOLATION_SENTINEL in r.upper()]

    def clear(self) -> None:
        self.calls.clear()


class FakeImageProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        n: int,
        batch: bool,
        model: str | None = None,
    ) -> ImageGenerationResult:
        self.calls.append(
            {"prompt": prompt, "n": n, "aspect": aspect, "batch": batch, "model": model}
        )
        width, height = _size_for_aspect(aspect)
        variants = [
            ImageVariant(
                content=_render(prompt, reference_images, width, height, variant_index=i),
                mime="image/png",
                width=width,
                height=height,
            )
            for i in range(n)
        ]
        return ImageGenerationResult(
            variants=variants, provider="fake", model=model or "fake-image-1", latency_ms=8
        )

    def clear(self) -> None:
        self.calls.clear()


class FakeEmbeddingProvider:
    """Feature hashing, not a stub.

    Tokens are hashed into dimensions and the vector is L2-normalised, so two
    texts that share vocabulary genuinely come out close and two that share
    none genuinely come out far apart. That matters more here than for the
    other fakes: `trends.services.clustering` groups by cosine distance, so a
    fake returning arbitrary vectors would make every clustering test a test of
    nothing. The trade-off is honest — no semantics, only overlap — which is
    the same trade-off the image fake makes by drawing a real PNG that is not a
    real photograph.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        # Imported here so the module keeps working without the trends app
        # installed — the other two fakes have no such dependency either.
        from trends.models import EMBEDDING_DIMENSIONS

        vector = [0.0] * EMBEDDING_DIMENSIONS
        # Content words only. Two posts about the same thing share their nouns,
        # not their articles — counting "the" would put every English sentence
        # within a hair of every other, which is the opposite of what a real
        # embedding does and would make the similarity threshold meaningless.
        for token in dict.fromkeys(content_tokens(text)):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            # The sign bit spreads tokens across the space instead of piling
            # every one into the positive orthant, where everything correlates.
            sign = 1.0 if digest[4] % 2 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # An empty or punctuation-only body still needs a unit vector, or
            # cosine distance is undefined against it.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def clear(self) -> None:
        self.calls.clear()


# Module-level so tests can inspect calls after a service/view has run
# (mirrors `billing.gateways.fake._fake_gateway`).
_fake_text_provider = FakeTextProvider()
_fake_image_provider = FakeImageProvider()
_fake_embedding_provider = FakeEmbeddingProvider()
