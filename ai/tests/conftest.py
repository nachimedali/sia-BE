"""Fixtures shared by the ai/ suite."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def workspace(workspace: Any) -> Any:
    """Overrides the root fixture with one that actually has credits.

    Provisioning a workspace assigns the Free plan but writes no ledger row —
    the grant is due, derived from the ledger, not issued at signup (A40) —
    so every other app's tests that spend credits do the same top-up
    (billing/tests/test_ledger.py etc.)."""
    from billing.services.ledger import grant_credits

    grant_credits(workspace, 100, note="test funding")
    return workspace


@pytest.fixture
def product(workspace: Any) -> Any:
    from products.services.products import create_product

    return create_product(workspace=workspace, name="Aurora Ceramic Mug")


@pytest.fixture
def product_with_reference_image(product: Any, make_png_upload: Any) -> Any:
    from products.services.products import attach_reference_images

    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    return product


@pytest.fixture
def voice_profile(workspace: Any) -> Any:
    from ai.models import VoiceProfile

    return VoiceProfile.objects.create(
        workspace=workspace,
        name="Default",
        tone_descriptors=["warm", "direct"],
        banned_phrases=["synergy"],
        system_prompt="Write like a small, proud, independent brand.",
    )
