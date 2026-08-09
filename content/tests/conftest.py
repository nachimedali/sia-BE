"""Fixtures shared by the content suite."""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image


@pytest.fixture
def make_png_upload() -> Any:
    def _make(name: str = "photo.png", size: tuple[int, int] = (600, 600)) -> SimpleUploadedFile:
        buffer = io.BytesIO()
        Image.new("RGB", size, color=(120, 130, 200)).save(buffer, format="PNG")
        return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")

    return _make


@pytest.fixture
def media_asset(workspace: Any, make_png_upload: Any) -> Any:
    from content.services.media import ingest_media

    return ingest_media(workspace=workspace, upload=make_png_upload())
