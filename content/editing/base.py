"""`ImageEditPort` / `VideoEditPort` (P1-12).

Two ports rather than one, for the same reason `MetricsProvider` and
`PlatformAdapter` are separate (P0-28): cropping a still and re-encoding a clip
are different vendors, different failure modes and different costs, and a
single `MediaEditPort` carrying both is what makes swapping either one an
excavation.

**Neither port mutates anything.** They take bytes and return bytes; deciding
that the result becomes a new `MediaAsset` with `derived_from` is
`content.services.editing`'s job. A port that wrote to the database would be a
port that had to know about workspaces, and immutability would then depend on
every adapter remembering it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CropBox:
    """Pixels, top-left origin. Explicit rather than a 4-tuple because
    `(0, 0, 300, 300)` is ambiguous between (left, top, width, height) and
    (left, top, right, bottom), and the two differ silently."""

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class EditedMedia:
    """What an editor hands back. `content` rather than a path or a URL: the
    ingestion path stores bytes and mints its own signed URL, so an editor
    returning a location would put a second source of truth for the same asset
    into the system — the same reasoning as `VideoResult.content`."""

    content: bytes
    mime: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


class ImageEditPort(Protocol):
    def crop(self, *, content: bytes, box: CropBox) -> EditedMedia: ...


class VideoEditPort(Protocol):
    def trim(self, *, content: bytes, start_ms: int, end_ms: int) -> EditedMedia: ...
