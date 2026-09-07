"""Crop and trim (P1-12).

**A new ingestion path, not a mutation path.** `MediaAsset` is immutable by
design, so an edit produces a *new* asset carrying `derived_from` and leaves
the original exactly as it was. That is not a workaround for immutability — it
is what keeps "which file did we actually publish" answerable a year later,
after the crop has been redone twice.

The ports do the pixels; this module owns the decisions immutability depends
on: which workspace the result belongs to, what it is derived from, and that
nothing about the source row changes.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from common.exceptions import OCCSError
from content.editing.base import CropBox
from content.editing.resolve import get_image_editor, get_video_editor
from content.models import MediaAsset, MediaKind, MediaSource
from content.services.media import ingest_media


class InvalidEditError(OCCSError):
    """A 400: the edit itself does not make sense — a zero-width crop, a box
    that runs off the image, a trim that ends before it starts."""

    default_code = "invalid_edit"
    default_detail = "This edit cannot be applied to that media."


class EditorNotConfiguredError(OCCSError):
    """A 422, not a 400 and not a 402. The request is fine and no plan fixes
    it; this deployment simply has no vendor behind the port — the same answer
    `create_generation` gives for video."""

    status_code = 422
    default_code = "editor_not_configured"
    default_detail = "No media editor is configured for this deployment."


def _read(asset: MediaAsset) -> bytes:
    with asset.file.open("rb") as handle:
        content: bytes = handle.read()
    return content


def _store(source: MediaAsset, *, content: bytes, mime: str, filename: str) -> MediaAsset:
    """Ingests the edited bytes as a new asset.

    Through `ingest_media` rather than `MediaAsset.objects.create`, so the
    result is probed, checksummed and size-checked exactly like an upload —
    an edit that produced a corrupt file should fail the same gate a corrupt
    upload does, not bypass it because it came from inside the system.
    """
    derived = ingest_media(
        workspace=source.workspace,
        upload=SimpleUploadedFile(filename, content, content_type=mime),
        source=MediaSource.DERIVED,
    )
    derived.derived_from = source
    derived.save(update_fields=["derived_from"])
    return derived


def crop_image(asset: MediaAsset, *, box: CropBox) -> MediaAsset:
    if asset.kind != MediaKind.IMAGE:
        raise InvalidEditError("Only images can be cropped.", detail={"media_asset": asset.pk})
    if box.width <= 0 or box.height <= 0 or box.left < 0 or box.top < 0:
        raise InvalidEditError(
            "A crop box needs a positive width and height inside the image.",
            detail={"box": [box.left, box.top, box.width, box.height]},
        )
    runs_off = (
        asset.width
        and asset.height
        and (box.left + box.width > asset.width or box.top + box.height > asset.height)
    )
    if runs_off:
        raise InvalidEditError(
            f"That crop runs off a {asset.width}x{asset.height} image.",
            detail={
                "box": [box.left, box.top, box.width, box.height],
                "size": [asset.width, asset.height],
            },
        )

    editor = get_image_editor()
    if editor is None:  # pragma: no cover — Pillow is always available
        raise EditorNotConfiguredError(detail={"port": "ImageEditPort"})

    result = editor.crop(content=_read(asset), box=box)
    return _store(asset, content=result.content, mime=result.mime, filename=f"crop-{asset.pk}.png")


def trim_video(asset: MediaAsset, *, start_ms: int, end_ms: int) -> MediaAsset:
    if asset.kind != MediaKind.VIDEO:
        raise InvalidEditError("Only video can be trimmed.", detail={"media_asset": asset.pk})
    if start_ms < 0 or end_ms <= start_ms:
        raise InvalidEditError(
            "A trim needs an end after its start.",
            detail={"start_ms": start_ms, "end_ms": end_ms},
        )

    editor = get_video_editor()
    if editor is None:
        raise EditorNotConfiguredError(detail={"port": "VideoEditPort"})

    result = editor.trim(content=_read(asset), start_ms=start_ms, end_ms=end_ms)
    derived = _store(
        asset, content=result.content, mime=result.mime, filename=f"trim-{asset.pk}.mp4"
    )
    if result.duration_ms is not None:
        derived.duration_ms = result.duration_ms
        derived.save(update_fields=["duration_ms"])
    return derived
