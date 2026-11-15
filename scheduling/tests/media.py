"""A real, ingested image for tests that auto-publish to the Instagram test
account, which publishes nothing without media (`ensure_media_fits`)."""

from __future__ import annotations

import io
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image


def an_image(workspace: Any) -> Any:
    from content.services.media import ingest_media

    buffer = io.BytesIO()
    Image.new("RGB", (1080, 1080), color=(120, 130, 200)).save(buffer, format="PNG")
    upload = SimpleUploadedFile("photo.png", buffer.getvalue(), content_type="image/png")
    return ingest_media(workspace=workspace, upload=upload)
