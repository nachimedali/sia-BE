"""Media ingestion (design.md §6.3/§6.4, implementation.md Phase 4.2).

Checksum, dimensions and mime are established here from the file itself, never
trusted from the client's declared content-type. This is a coarse sanity gate
— file too large, or an image so extreme in aspect ratio nothing could use it
— distinct from the Adaptation Engine's per-platform aspect/media-type
compliance, which runs later at preview/publish time and drops or substitutes
rather than rejecting outright (design.md A53).

Video is accepted by declared MIME type only: dimensions and duration are left
null until a later phase adds a media prober, which this stack does not carry
yet.
"""

from __future__ import annotations

import hashlib
import mimetypes
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError

from common.exceptions import OCCSError
from common.media import probe_image_integrity
from content.models import MediaAsset, MediaKind, MediaSource
from workspaces.models import Workspace

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — generous for a single social asset.
MIN_ASPECT_RATIO = 0.2  # 1:5
MAX_ASPECT_RATIO = 5.0  # 5:1 — outside this, no platform can meaningfully use it.

_IMAGE_MIME_PREFIX = "image/"
_VIDEO_MIME_PREFIX = "video/"
_ALLOWED_VIDEO_MIME = frozenset({"video/mp4", "video/quicktime", "video/webm"})


class UnsupportedMediaError(OCCSError):
    default_code = "unsupported_media"
    default_detail = "This file cannot be used as media."


def _sha256(upload: UploadedFile[Any]) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def _sniff_content_type(upload: UploadedFile[Any]) -> str:
    declared = (upload.content_type or "").lower()
    guessed = mimetypes.guess_type(upload.name or "")[0] or ""
    return declared or guessed


def ingest_media(
    *, workspace: Workspace, upload: UploadedFile[Any], source: str = MediaSource.UPLOAD
) -> MediaAsset:
    if upload.size is not None and upload.size > MAX_FILE_SIZE_BYTES:
        raise UnsupportedMediaError(
            f"File is too large ({upload.size} bytes); the limit is {MAX_FILE_SIZE_BYTES} bytes.",
            code="file_too_large",
        )

    content_type = _sniff_content_type(upload)
    width = height = duration_ms = None

    if content_type.startswith(_VIDEO_MIME_PREFIX):
        if content_type not in _ALLOWED_VIDEO_MIME:
            raise UnsupportedMediaError(
                f"Unsupported video type: {content_type}.", code="invalid_video"
            )
        kind = MediaKind.VIDEO
        mime = content_type
    else:
        # Anything not declared as video is validated as an image by actually
        # opening it — a declared content-type is only a hint, and an empty one
        # (some clients send none) still has to resolve to something.
        integrity_ok, _detail = probe_image_integrity(upload)
        if not integrity_ok:
            raise UnsupportedMediaError(
                f"Unsupported file type: {content_type or 'unknown'}.", code="invalid_media_type"
            )
        upload.seek(0)
        try:
            with Image.open(upload) as image:
                width, height = image.size
                mime = Image.MIME.get(image.format or "", content_type or "image/*")
            upload.seek(0)
        except (UnidentifiedImageError, OSError) as exc:
            raise UnsupportedMediaError(
                f"Unsupported file type: {content_type or 'unknown'}.", code="invalid_media_type"
            ) from exc

        aspect = width / height if height else None
        if aspect is not None and not (MIN_ASPECT_RATIO <= aspect <= MAX_ASPECT_RATIO):
            raise UnsupportedMediaError(
                f"Image aspect ratio {aspect:.2f} is outside the usable range "
                f"({MIN_ASPECT_RATIO}-{MAX_ASPECT_RATIO}).",
                code="aspect_ratio_rejected",
            )
        kind = MediaKind.IMAGE

    checksum = _sha256(upload)

    return MediaAsset.objects.create(
        workspace=workspace,
        kind=kind,
        file=upload,
        mime=mime,
        width=width,
        height=height,
        duration_ms=duration_ms,
        checksum=checksum,
        source=source,
    )
