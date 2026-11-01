"""The product's address, its new list filters and its generated images.

`/app/products/olive-oil-3f9a2c1e`: a readable slug plus the row's random
`ref`, so the integer pk never appears in a URL. The lookup goes by `ref`
alone — renaming a product changes its slug, never which product it opens.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from ai.models import (
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationStatus,
    GenerationVariant,
)
from content.models import Post, PostStatus
from products.models import Product
from taste.models import CandidateState, ContentCandidate, TasteProfile
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PRODUCTS_URL = "/api/v1/products/"


def _make(workspace: Any, name: str, **fields: Any) -> Product:
    return Product.objects.create(workspace=workspace, name=name, **fields)


def _names(response: Any) -> set[str]:
    assert response.status_code == 200, response.content
    return {p["name"] for p in response.json()["results"]}


def _stranger_product() -> Product:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    return _make(provision_workspace(stranger, name="Elsewhere"), "Rival Oil")


def _sibling_product(user: Any, workspace: Any, plans: Any) -> Product:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    return _make(provision_extra_workspace(user=user, name="Sister Brand"), "Sister Oil")


def _generation(workspace: Any, user: Any, product: Product, **fields: Any) -> Generation:
    return Generation.objects.create(
        workspace=workspace,
        user=user,
        product=product,
        kind=fields.pop("kind", GenerationKind.IMAGE),
        mode=GenerationMode.PRODUCT,
        status=fields.pop("status", GenerationStatus.SUCCEEDED),
        **fields,
    )


# --- ref and slug -------------------------------------------------------------
def test_every_product_gets_a_ref_and_a_slug(auth_client: Any, workspace: Any) -> None:
    product = _make(workspace, "Huile d'Olive — Première Récolte")

    body = auth_client.get(f"{PRODUCTS_URL}{product.pk}/").json()

    assert len(body["ref"]) == 8
    assert body["slug"] == f"huile-dolive-premiere-recolte-{body['ref']}"


def test_a_name_with_no_letters_still_has_a_slug(workspace: Any) -> None:
    product = _make(workspace, "🫒🫒")

    assert product.slug == f"product-{product.ref}"


def test_refs_differ_between_products(workspace: Any) -> None:
    assert _make(workspace, "A").ref != _make(workspace, "B").ref


def test_by_ref_returns_the_product(auth_client: Any, workspace: Any) -> None:
    product = _make(workspace, "Olive Oil")

    response = auth_client.get(f"{PRODUCTS_URL}by-ref/{product.ref}/")

    assert response.status_code == 200, response.content
    assert response.json()["id"] == product.pk
    assert "post_count" in response.json()


def test_by_ref_unknown_is_404(auth_client: Any, workspace: Any) -> None:
    _make(workspace, "Olive Oil")

    assert auth_client.get(f"{PRODUCTS_URL}by-ref/00000000/").status_code == 404


def test_by_ref_malformed_is_404(auth_client: Any) -> None:
    assert auth_client.get(f"{PRODUCTS_URL}by-ref/42/").status_code == 404


def test_by_ref_another_organization_is_404(auth_client: Any, workspace: Any) -> None:
    assert auth_client.get(f"{PRODUCTS_URL}by-ref/{_stranger_product().ref}/").status_code == 404


def test_by_ref_a_sibling_workspace_is_404(
    auth_client: Any, user: Any, workspace: Any, plans: Any
) -> None:
    sibling = _sibling_product(user, workspace, plans)

    response = auth_client.get(
        f"{PRODUCTS_URL}by-ref/{sibling.ref}/", HTTP_X_WORKSPACE_ID=str(workspace.id)
    )

    assert response.status_code == 404


# --- several platforms at once -------------------------------------------------
@pytest.fixture
def catalogue(workspace: Any) -> dict[str, Product]:
    return {
        "serum": _make(workspace, "Serum", platforms=["instagram", "tiktok"]),
        "mug": _make(workspace, "Mug", platforms=["pinterest"]),
        "shirt": _make(workspace, "Shirt", platforms=["facebook"]),
    }


def test_platform_filter_takes_several_and_matches_any(auth_client: Any, catalogue: Any) -> None:
    response = auth_client.get(PRODUCTS_URL, {"platform": ["tiktok", "pinterest"]})

    assert _names(response) == {"Serum", "Mug"}


def test_one_unknown_platform_among_several_is_rejected(auth_client: Any, catalogue: Any) -> None:
    response = auth_client.get(PRODUCTS_URL, {"platform": ["tiktok", "myspace"]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_platform"


# --- generated / requires check -------------------------------------------------
def test_generated_means_a_succeeded_generation(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any
) -> None:
    _generation(workspace, user, catalogue["serum"])
    _generation(workspace, user, catalogue["mug"], status=GenerationStatus.FAILED)

    assert _names(auth_client.get(PRODUCTS_URL, {"status": "generated"})) == {"Serum"}


def test_requires_check_means_a_post_or_candidate_waiting_for_review(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any
) -> None:
    Post.objects.create(
        workspace=workspace,
        author=user,
        product=catalogue["serum"],
        status=PostStatus.PENDING_REVIEW,
    )
    Post.objects.create(
        workspace=workspace, author=user, product=catalogue["shirt"], status=PostStatus.DRAFT
    )
    profile = TasteProfile.objects.create(workspace=workspace, version=1)
    ContentCandidate.objects.create(
        workspace=workspace,
        taste_profile=profile,
        product=catalogue["mug"],
        state=CandidateState.PENDING_REVIEW,
    )

    assert _names(auth_client.get(PRODUCTS_URL, {"status": "requires_check"})) == {"Serum", "Mug"}


def test_summary_counts_generated_and_requires_check(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any
) -> None:
    _generation(workspace, user, catalogue["serum"])
    _generation(workspace, user, catalogue["serum"])  # two runs, still one product
    Post.objects.create(
        workspace=workspace, author=user, product=catalogue["mug"], status=PostStatus.PENDING_REVIEW
    )

    body = auth_client.get(f"{PRODUCTS_URL}summary/").json()

    assert body["generated"] == 1
    assert body["requires_check"] == 1


# --- generated images ------------------------------------------------------------
def test_generated_images_lists_image_variants_newest_first(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any, media_asset: Any
) -> None:
    serum = catalogue["serum"]
    older = _generation(workspace, user, serum)
    GenerationVariant.objects.create(generation=older, kind="IMAGE", media_asset=media_asset)
    newer = _generation(workspace, user, serum)
    keep = GenerationVariant.objects.create(
        generation=newer, kind="IMAGE", media_asset=media_asset, was_selected=True, headline="Gold"
    )
    # Neither of these is an image the tab can show.
    GenerationVariant.objects.create(generation=newer, kind="TEXT", body="caption only")
    failed = _generation(workspace, user, serum, status=GenerationStatus.FAILED)
    GenerationVariant.objects.create(generation=failed, kind="IMAGE", media_asset=media_asset)

    response = auth_client.get(f"{PRODUCTS_URL}{serum.pk}/generated-images/")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 2
    first = body["results"][0]
    assert first["id"] == keep.pk
    assert first["generation"] == newer.pk
    assert first["was_selected"] is True
    assert first["headline"] == "Gold"
    assert first["media"]["id"] == media_asset.pk


def test_generated_images_empty_is_an_empty_page(auth_client: Any, catalogue: Any) -> None:
    body = auth_client.get(f"{PRODUCTS_URL}{catalogue['mug'].pk}/generated-images/").json()

    assert body["count"] == 0
    assert body["results"] == []


def test_generated_images_another_organization_is_404(auth_client: Any, workspace: Any) -> None:
    url = f"{PRODUCTS_URL}{_stranger_product().pk}/generated-images/"

    assert auth_client.get(url).status_code == 404


def test_generated_images_a_sibling_workspace_is_404(
    auth_client: Any, user: Any, workspace: Any, plans: Any
) -> None:
    sibling = _sibling_product(user, workspace, plans)

    response = auth_client.get(
        f"{PRODUCTS_URL}{sibling.pk}/generated-images/", HTTP_X_WORKSPACE_ID=str(workspace.id)
    )

    assert response.status_code == 404
