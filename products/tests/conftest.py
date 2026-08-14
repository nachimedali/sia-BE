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


# --- autopilot ---------------------------------------------------------------
# The engine's body is generation, so its tests need everything `ai/tests`
# needs: the fakes (A8, root conftest's `_fake_ai_providers`/`_clear_fake_
# providers`), a seeded cost table (root conftest's `generation_costs`),
# credits to spend and a product that satisfies I7.


@pytest.fixture
def autopilot_workspace(workspace: Any, plans: dict[str, Any]) -> Any:
    """Pro: autopilot is paid (§4.1), and Pro is the tier that has autopilot
    without `autopilot_auto_approve` — which is what makes the auto-approve
    gate test meaningful rather than tautological."""
    from billing.services.ledger import grant_credits, grant_video_units
    from channels.models import SocialAccount

    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    grant_credits(workspace, 100, note="test funding")
    grant_video_units(workspace, plans["pro"].included_videos, note="test allowance")
    # Autopilot exists to fill a calendar that publishes itself, so the default
    # fixture has somewhere to publish to. The no-account case is its own test.
    SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        handle="@acme",
        display_name="Acme Studio",
        provider_account_id="acct-autopilot-1",
    )
    return workspace


@pytest.fixture
def autopilot_product(autopilot_workspace: Any, make_png_upload: Any) -> Any:
    from products.services.products import attach_reference_images, create_product

    product = create_product(
        workspace=autopilot_workspace,
        name="Aurora Ceramic Mug",
        restrictions=["Never show the mug empty."],
        platforms=["instagram"],
    )
    attach_reference_images(product=product, uploads=[make_png_upload()])
    product.refresh_from_db()
    return product


@pytest.fixture
def autopilot_config(autopilot_product: Any, generation_costs: None) -> Any:
    from products.models import AutopilotConfig

    return AutopilotConfig.objects.create(product=autopilot_product, enabled=True)
