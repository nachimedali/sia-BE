"""The two outside readers the pre-publish checks need (steps-plan S4).

`OcrPort` reads the words in a picture; `PolicyPort` reads a caption against a
platform's advertising and community rules. Both are ports with a fake (Part 7
rule 6), and both may legitimately resolve to **nothing**: a deployment with no
vendor configured reports those checks as *unavailable* — never as passed,
because a check that did not run is not a check that found nothing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings

from ai.providers.switches import uses_fake


@dataclass(frozen=True)
class TextBox:
    """One run of text found in an image. Position and size are fractions of
    the image (0-1), so a rule can be stated without knowing its pixels."""

    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class OcrResult:
    boxes: list[TextBox]
    provider: str


class OcrPort(Protocol):
    def read(self, image: bytes) -> OcrResult: ...


@dataclass(frozen=True)
class PolicyFinding:
    """One rule a caption runs into.

    `status` is `FIX` (allowed once changed) or `BLOCK` (not allowed at all).
    `clause` is the rule, in the words the provider gives it; `source` is where
    it is published; `rewrite` keeps the post's intent without the problem.
    """

    status: str
    title: str
    clause: str
    source: str
    excerpt: str
    rewrite: str


@dataclass(frozen=True)
class PolicyResult:
    findings: list[PolicyFinding] = field(default_factory=list)
    provider: str = ""


class PolicyPort(Protocol):
    def review(self, *, text: str, platform: str, category: str) -> PolicyResult: ...


@dataclass(frozen=True)
class VideoFacts:
    """What was measured in one video: times are seconds from the start, the
    two luma figures run 0 (black) to 1 (white), and `motion` is how much the
    picture changed over the opening second (0 none .. 1 a different picture)."""

    duration: float
    has_audio: bool
    black: list[tuple[float, float]]
    silent: list[tuple[float, float]]
    first_luma: float
    motion: float
    last_frame: bytes | None
    provider: str


class VideoPort(Protocol):
    def inspect(self, path: str) -> VideoFacts: ...


_video_override: VideoPort | None = None


def set_video_override(port: VideoPort | None) -> None:
    global _video_override
    _video_override = port


def get_video() -> VideoPort | None:
    """The frame reader: `ffmpeg` on this machine, or nothing — in which case
    the video checks report *Not checked* rather than pass."""
    if _video_override is not None:
        return _video_override
    if uses_fake("CHECKS"):
        from checks.fakes import FakeVideo

        return FakeVideo()
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return None
    from checks.video import FfmpegVideo

    return FfmpegVideo()


_ocr_override: OcrPort | None = None
_policy_override: PolicyPort | None = None


def set_ocr_override(port: OcrPort | None) -> None:
    """Tests only: swap the reader for one that sees what the test needs."""
    global _ocr_override
    _ocr_override = port


def set_policy_override(port: PolicyPort | None) -> None:
    global _policy_override
    _policy_override = port


def get_ocr() -> OcrPort | None:
    if _ocr_override is not None:
        return _ocr_override
    if uses_fake("CHECKS"):
        from checks.fakes import FakeOcr

        return FakeOcr()
    if not getattr(settings, "OCR_PROVIDER_API_KEY", ""):
        return None
    raise NotImplementedError("OCR_PROVIDER_API_KEY is set but no OCR adapter is implemented yet.")


def get_policy() -> PolicyPort | None:
    if _policy_override is not None:
        return _policy_override
    if uses_fake("CHECKS"):
        from checks.fakes import FakePolicy

        return FakePolicy()
    if not getattr(settings, "POLICY_PROVIDER_API_KEY", ""):
        return None
    raise NotImplementedError(
        "POLICY_PROVIDER_API_KEY is set but no policy adapter is implemented yet."
    )
