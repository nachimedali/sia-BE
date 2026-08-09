"""Fixtures shared by the products suite."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def product(workspace: Any) -> Any:
    from products.services.products import create_product

    return create_product(workspace=workspace, name="Aurora Ceramic Mug")


@pytest.fixture
def media_asset(workspace: Any, make_png_upload: Any) -> Any:
    from content.services.media import ingest_media

    return ingest_media(workspace=workspace, upload=make_png_upload())
