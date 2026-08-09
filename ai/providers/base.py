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
