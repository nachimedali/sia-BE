"""Shared image-decoding primitives used at more than one boundary (upload
ingestion in `content/`, the generation quality gate in `ai/`)."""

from __future__ import annotations

from typing import IO

from PIL import Image, UnidentifiedImageError


def probe_image_integrity(file_like: IO[bytes]) -> tuple[bool, str]:
    """True if Pillow can decode and verify `file_like` as an image, else
    `(False, <error detail>)`. Does not rewind the stream afterward — that is
    the caller's responsibility, since what they do next (reopen for pixel
    data, or nothing) differs by call site."""
    try:
        with Image.open(file_like) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        return False, str(exc)
    return True, ""
