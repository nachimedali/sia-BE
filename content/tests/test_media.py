"""Media ingestion (design.md §6.3/§6.4, implementation.md Phase 4.2)."""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from content.models import MediaKind, MediaSource
from content.services.media import MAX_FILE_SIZE_BYTES, UnsupportedMediaError, ingest_media

pytestmark = pytest.mark.django_db


def _png_upload(size: tuple[int, int], name: str = "x.png") -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def test_ingest_sniffs_checksum_dimensions_and_mime(workspace: Any) -> None:
    asset = ingest_media(workspace=workspace, upload=_png_upload((800, 600)))

    assert asset.kind == MediaKind.IMAGE
    assert asset.width == 800
    assert asset.height == 600
    assert asset.mime == "image/png"
    assert len(asset.checksum) == 64  # sha256 hex digest
    assert asset.source == MediaSource.UPLOAD
    assert asset.workspace_id == workspace.id


def test_media_rejected_on_aspect_or_size_violation(workspace: Any) -> None:
    # An aspect ratio no platform could use.
    extreme = _png_upload((2000, 50))
    with pytest.raises(UnsupportedMediaError):
        ingest_media(workspace=workspace, upload=extreme)

    # Oversized regardless of what it contains.
    oversized = SimpleUploadedFile(
        "big.png", b"0" * (MAX_FILE_SIZE_BYTES + 1), content_type="image/png"
    )
    with pytest.raises(UnsupportedMediaError):
        ingest_media(workspace=workspace, upload=oversized)


def test_a_reasonable_portrait_aspect_ratio_is_accepted(workspace: Any) -> None:
    tall = _png_upload((1080, 1350))  # 4:5 — a real Instagram portrait ratio.
    asset = ingest_media(workspace=workspace, upload=tall)
    assert asset.aspect_ratio == pytest.approx(1080 / 1350)


def test_corrupt_image_bytes_are_rejected(workspace: Any) -> None:
    garbage = SimpleUploadedFile("nope.png", b"not-actually-a-png", content_type="image/png")
    with pytest.raises(UnsupportedMediaError):
        ingest_media(workspace=workspace, upload=garbage)


def test_declared_video_mime_is_accepted_without_probing_dimensions(workspace: Any) -> None:
    upload = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")
    asset = ingest_media(workspace=workspace, upload=upload)

    assert asset.kind == MediaKind.VIDEO
    assert asset.mime == "video/mp4"
    assert asset.width is None
    assert asset.duration_ms is None


def test_unsupported_video_container_is_rejected(workspace: Any) -> None:
    upload = SimpleUploadedFile("clip.avi", b"RIFF....AVI ", content_type="video/x-msvideo")
    with pytest.raises(UnsupportedMediaError):
        ingest_media(workspace=workspace, upload=upload)
