"""`TextProvider` / `ImageProvider` ports (design.md §9, D8).

Every external dependency sits behind a port with a real adapter and a
deterministic fake (A8). Text and image share this module because both ports
are needed together from Phase 7 on and lean on the same shape of result.

Batch vs sync (D8) is a parameter on `ImageProvider.generate`, not two
methods: autopilot and Studio ask the same question ("generate me images"),
just with a different `batch` flag, and the pipeline that calls this port is
identical either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from common.exceptions import ProviderError


def unwrap_json_response(response: Any, *, label: str) -> dict[str, Any]:
    """Shared by every real adapter's HTTP calls: a non-2xx/3xx status becomes
    a `ProviderError` carrying the response body, otherwise the parsed JSON
    is returned as-is."""
    if response.status_code >= 400:
        raise ProviderError(
            f"{label} returned {response.status_code}.",
            detail={"body": response.text[:500]},
        )
    result: dict[str, Any] = response.json()
    return result


@dataclass(frozen=True)
class TextVariant:
    body: str
    rationale: str = ""


@dataclass(frozen=True)
class TextGenerationResult:
    variants: list[TextVariant]
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class TextProvider(Protocol):
    def generate(
        self, *, system: str, prompt: str, n: int, model: str | None = None
    ) -> TextGenerationResult: ...

    def caption(
        self, *, system: str, prompt: str, image_bytes: bytes, n: int, model: str | None = None
    ) -> TextGenerationResult:
        """Write `n` captions for an image (P1-13).

        A method on `TextProvider` rather than a fourth port: the vendor is the
        same LLM gateway, the result is the same `TextGenerationResult`, and
        `classify_constraints` below already established that this port sees
        images. A separate `VisionProvider` would be one more thing to
        configure for no substitutable behaviour behind it.
        """
        ...

    def classify_constraints(self, *, image_bytes: bytes, restrictions: list[str]) -> list[str]:
        """Which of `restrictions` this image violates — empty if none.

        design.md §5 describes the LLM provider's job as "text, labelling,
        synthesis"; this is the labelling half, reused for the quality gate's
        brand-constraint check (design.md §8.3) rather than standing up a
        second, vision-specific provider port for one check.
        """
        ...


@dataclass(frozen=True)
class ImageVariant:
    content: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageGenerationResult:
    variants: list[ImageVariant]
    provider: str
    model: str
    latency_ms: int
    warnings: list[str] = field(default_factory=list)


class ImageProvider(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        n: int,
        batch: bool,
        model: str | None = None,
    ) -> ImageGenerationResult: ...


@dataclass(frozen=True)
class VideoResult:
    """One rendered clip.

    `content` rather than a URL: the ingestion path stores bytes and mints its
    own signed URL, so a provider handing back a link would put a second,
    expiring source of truth for the same asset into the system.
    """

    content: bytes
    mime: str
    duration_seconds: float
    provider: str
    model: str
    latency_ms: int


class VideoProvider(Protocol):
    """Video generation (C-11 / P0-04).

    **The port exists; no paid vendor sits behind it.** C-11's complaint was
    that video was "gated correctly, no provider" — a gate in front of nothing.
    Two options were open: remove the gate, or put a port with a fake behind
    it so a fresh checkout runs end to end (Part 7 rule 6). The port is the
    better of the two, because the gate itself is correct and deleting it would
    have to be undone the day a vendor is chosen.

    In production `get_video_provider()` resolves to `None` and the pipeline
    says so plainly, rather than a fake quietly producing a clip nobody
    rendered.
    """

    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        duration_seconds: float,
        model: str | None = None,
    ) -> VideoResult: ...


class EmbeddingProvider(Protocol):
    """Text → vector, for the trend clustering in design.md §8.4.

    A third AI port rather than a method on `TextProvider`: embeddings are a
    different endpoint, a different model and a different price, and the one
    caller (`trends.services.clustering`) has no use for chat completion. It
    lands here rather than in `trends/providers/` because the vendor is the
    same class of dependency the other two have — an LLM gateway, configured
    by `LLM_BASE_URL` — while a `TrendVendor` is a data source.
    """

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """One vector per input, in the order given. Implementations must return
        exactly `len(texts)` vectors, each of `EMBEDDING_DIMENSIONS` width."""
        ...
