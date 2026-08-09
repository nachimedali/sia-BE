"""I7 — the future `/ai/generate/` (Phase 7) gate, built now (design.md §15.6
A54). `test_generation_rejected_without_reference_image` is one of
implementation.md's named invariant tests, listed under both Phase 5 and the
§5 testing contract table.
"""

from __future__ import annotations

from typing import Any

import pytest

from products.services.guards import ProductNotGenerationReadyError, ensure_generation_ready
from products.services.products import attach_reference_images

pytestmark = pytest.mark.django_db


def test_generation_rejected_without_reference_image(product: Any) -> None:
    assert product.is_generation_ready is False
    with pytest.raises(ProductNotGenerationReadyError) as excinfo:
        ensure_generation_ready(product)
    assert excinfo.value.status_code == 400


def test_generation_allowed_once_a_reference_image_is_attached(
    product: Any, make_png_upload: Any
) -> None:
    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    ensure_generation_ready(product)  # must not raise
