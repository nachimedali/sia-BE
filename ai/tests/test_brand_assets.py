"""Brand assets on an image generation: the person's own pictures beside the
product's — a logo, the packaging, anything the render must carry.

A brand asset is **not** a product reference. The product's photographs are
what the quality gate holds the render's identity to; a logo or a box is
something the render must *include*, and scoring identity against it would
fail every render that shows the product rather than the logo. So assets ride
to the provider after the product's photographs (and after a composition's
references, whose numbering they must not shift), each with a line saying what
it is, and never reach the gate.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from ai.models import Generation, GenerationKind, GenerationMode, GenerationStatus
from ai.providers.fake import _fake_image_provider
from ai.services import regeneration
from ai.services.pipeline import create_generation
from content.models import MediaAsset, MediaKind
from content.services.media import ingest_media
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

GENERATE_URL = "/api/v1/ai/generate/"


def _image_body(**extra: Any) -> dict[str, Any]:
    return {"kind": "IMAGE", "mode": "IDEA", "prompt": "a box on a marble counter", "n": 1, **extra}


@pytest.fixture
def logo(workspace: Any, make_png_upload: Any) -> MediaAsset:
    return ingest_media(workspace=workspace, upload=make_png_upload())


@pytest.fixture
def package(workspace: Any, make_png_upload: Any) -> MediaAsset:
    return ingest_media(workspace=workspace, upload=make_png_upload())


# --- the happy path ------------------------------------------------------------
def test_assets_reach_the_provider_after_the_product_photographs(
    auth_client: Any,
    product_with_reference_image: Any,
    logo: MediaAsset,
    package: MediaAsset,
    generation_costs: Any,
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        _image_body(
            mode="PRODUCT",
            product=product_with_reference_image.id,
            brand_assets=[
                {"media": logo.id, "role": "logo"},
                {"media": package.id, "role": "packaging", "note": "Show it opened"},
            ],
        ),
        format="json",
    )

    assert response.status_code == 201, response.content
    assert response.json()["status"] == GenerationStatus.SUCCEEDED
    call = _fake_image_provider.calls[-1]
    # One product photograph, then the two assets.
    assert call["reference_count"] == 3
    assert "Image 2 is the brand's logo" in call["prompt"]
    assert "Image 3 is the product's packaging" in call["prompt"]
    assert "Show it opened" in call["prompt"]


def test_assets_are_recorded_on_the_generation(
    auth_client: Any, logo: MediaAsset, generation_costs: Any
) -> None:
    response = auth_client.post(
        GENERATE_URL, _image_body(brand_assets=[{"media": logo.id, "role": "logo"}]), format="json"
    )

    assert response.status_code == 201, response.content
    generation = Generation.objects.get(pk=response.json()["id"])
    assert generation.creative["assets"] == [{"media": logo.id, "role": "logo", "note": ""}]


def test_without_a_product_the_assets_are_the_only_images(
    auth_client: Any, logo: MediaAsset, generation_costs: Any
) -> None:
    response = auth_client.post(
        GENERATE_URL, _image_body(brand_assets=[{"media": logo.id, "role": "logo"}]), format="json"
    )

    assert response.status_code == 201, response.content
    call = _fake_image_provider.calls[-1]
    assert call["reference_count"] == 1
    assert "Image 1 is the brand's logo" in call["prompt"]


def test_no_assets_changes_nothing(auth_client: Any, workspace: Any, generation_costs: Any) -> None:
    response = auth_client.post(GENERATE_URL, _image_body(), format="json")

    assert response.status_code == 201, response.content
    assert "assets" not in Generation.objects.get(pk=response.json()["id"]).creative
    assert "brand's logo" not in _fake_image_provider.calls[-1]["prompt"]


def test_the_quality_gate_never_scores_identity_against_an_asset(
    auth_client: Any,
    product_with_reference_image: Any,
    logo: MediaAsset,
    generation_costs: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai.services import quality

    seen: list[int] = []
    real = quality.run_image_quality_gate

    def spy(**kwargs: Any) -> Any:
        seen.append(len(kwargs["reference_images"]))
        return real(**kwargs)

    monkeypatch.setattr(quality, "run_image_quality_gate", spy)
    auth_client.post(
        GENERATE_URL,
        _image_body(
            mode="PRODUCT",
            product=product_with_reference_image.id,
            brand_assets=[{"media": logo.id, "role": "logo"}],
        ),
        format="json",
    )

    assert seen and set(seen) == {1}


def test_a_regeneration_keeps_the_assets(
    workspace: Any, user: Any, logo: MediaAsset, generation_costs: Any
) -> None:
    from content.models import Post

    parent = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.IDEA,
        prompt="a box",
        creative={"assets": [{"media": logo.id, "role": "logo", "note": ""}]},
    )
    post = Post.objects.create(workspace=workspace, author=user, master_body="x", generation=parent)

    child = regeneration.regenerate(post, user=user)

    assert child.creative["assets"] == [{"media": logo.id, "role": "logo", "note": ""}]


# --- every error -------------------------------------------------------------------
@pytest.mark.parametrize(
    "assets",
    [
        [{"media": 1, "role": "mascot"}],
        [{"role": "logo"}],
        [{"media": 1, "role": "logo", "note": "x" * 141}],
        "logo",
    ],
)
def test_a_malformed_asset_is_a_400(
    auth_client: Any, logo: MediaAsset, generation_costs: Any, assets: Any
) -> None:
    response = auth_client.post(GENERATE_URL, _image_body(brand_assets=assets), format="json")
    assert response.status_code == 400


def test_more_than_three_assets_is_a_400(
    auth_client: Any, workspace: Any, make_png_upload: Any, generation_costs: Any
) -> None:
    media = [ingest_media(workspace=workspace, upload=make_png_upload()) for _ in range(4)]
    response = auth_client.post(
        GENERATE_URL,
        _image_body(brand_assets=[{"media": item.id, "role": "other"} for item in media]),
        format="json",
    )
    assert response.status_code == 400


def test_the_same_asset_twice_is_a_400(
    auth_client: Any, logo: MediaAsset, generation_costs: Any
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        _image_body(
            brand_assets=[{"media": logo.id, "role": "logo"}, {"media": logo.id, "role": "other"}]
        ),
        format="json",
    )
    assert response.status_code == 400


def test_assets_on_a_text_generation_are_a_400(
    auth_client: Any, logo: MediaAsset, generation_costs: Any
) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {
            "kind": "TEXT",
            "mode": "IDEA",
            "prompt": "hi",
            "brand_assets": [{"media": logo.id, "role": "logo"}],
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_brand_asset"


def test_a_video_asset_is_a_400(auth_client: Any, logo: MediaAsset, generation_costs: Any) -> None:
    MediaAsset.objects.filter(pk=logo.pk).update(kind=MediaKind.VIDEO)
    response = auth_client.post(
        GENERATE_URL, _image_body(brand_assets=[{"media": logo.id, "role": "logo"}]), format="json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_brand_asset"


# --- tenancy: another organization's or another brand's picture is not yours ----
def test_an_asset_from_another_organization_is_refused(
    auth_client: Any, workspace: Any, make_png_upload: Any, generation_costs: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = ingest_media(
        workspace=provision_workspace(stranger, name="Theirs"), upload=make_png_upload()
    )

    response = auth_client.post(
        GENERATE_URL,
        _image_body(brand_assets=[{"media": theirs.id, "role": "logo"}]),
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_brand_asset"
    assert not Generation.objects.exists()


def test_an_asset_from_a_sibling_brand_is_refused(
    auth_client: Any,
    workspace: Any,
    user: Any,
    plans: Any,
    make_png_upload: Any,
    generation_costs: Any,
) -> None:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    sibling = provision_extra_workspace(user=user, name="Sister Brand")
    theirs = ingest_media(workspace=sibling, upload=make_png_upload())

    response = auth_client.post(
        GENERATE_URL,
        _image_body(brand_assets=[{"media": theirs.id, "role": "logo"}]),
        format="json",
        HTTP_X_WORKSPACE_ID=str(workspace.id),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_brand_asset"
