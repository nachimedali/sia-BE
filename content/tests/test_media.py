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


# -----------------------------------------------------------------------------
# Documents — LinkedIn's PDF carousel (P4-04)
# -----------------------------------------------------------------------------
def _pdf_upload(name: str = "deck.pdf", body: bytes = b"%PDF-1.7\nstub") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def test_a_pdf_is_ingested_as_a_document(workspace: Any) -> None:
    asset = ingest_media(workspace=workspace, upload=_pdf_upload())

    assert asset.kind == MediaKind.DOCUMENT
    assert asset.mime == "application/pdf"
    # No dimensions to probe and no aspect ratio to reject — a document is not
    # a picture, which is exactly why it needed its own kind rather than being
    # squeezed through the image branch.
    assert asset.width is None
    assert asset.height is None


def test_a_pdf_is_recognised_by_its_bytes_not_its_name(workspace: Any) -> None:
    """The rule this whole module is built on. A file claiming to be a PDF and
    containing a PNG is a PNG, whatever the client said."""
    disguised = SimpleUploadedFile("deck.pdf", _png_upload((60, 60)).read(), "application/pdf")

    asset = ingest_media(workspace=workspace, upload=disguised)

    assert asset.kind == MediaKind.IMAGE


def test_a_file_claiming_to_be_a_pdf_with_neither_header_is_refused(workspace: Any) -> None:
    # Not a PDF by its bytes, and not an image either — it falls through to
    # the image branch and is rejected there rather than stored as a document.
    junk = SimpleUploadedFile("deck.pdf", b"not a pdf at all", "application/pdf")

    with pytest.raises(UnsupportedMediaError):
        ingest_media(workspace=workspace, upload=junk)


def test_a_document_still_gets_a_checksum(workspace: Any) -> None:
    # The header read must seek back, or the checksum would cover only the
    # bytes left after it — and two different decks would collide.
    first = ingest_media(workspace=workspace, upload=_pdf_upload(body=b"%PDF-1.7\nalpha"))
    second = ingest_media(workspace=workspace, upload=_pdf_upload(body=b"%PDF-1.7\nbeta"))

    assert first.checksum
    assert first.checksum != second.checksum
