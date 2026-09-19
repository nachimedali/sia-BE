"""`DELETE /products/{id}/reference-images/{asset_id}/` — the manage page's
remove button. Detaching unlinks the asset from this product; the `MediaAsset`
itself is immutable and stays (other posts may use it)."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from content.models import MediaAsset
from content.services.media import ingest_media
from products.models import Product
from products.services.products import attach_reference_images
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db


def _url(product_id: int, asset_id: int) -> str:
    return f"/api/v1/products/{product_id}/reference-images/{asset_id}/"


@pytest.fixture
def attached(product: Any, make_png_upload: Any) -> list[MediaAsset]:
    return attach_reference_images(
        product=product, uploads=[make_png_upload("a.png"), make_png_upload("b.png")]
    )


def test_detach_returns_the_updated_product(
    auth_client: Any, product: Any, attached: list[MediaAsset]
) -> None:
    response = auth_client.delete(_url(product.id, attached[0].id))

    assert response.status_code == 200
    body = response.json()
    assert [asset["id"] for asset in body["reference_images"]] == [attached[1].id]
    assert body["is_generation_ready"] is True


def test_detaching_the_last_reference_locks_generation(
    auth_client: Any, product: Any, attached: list[MediaAsset]
) -> None:
    auth_client.delete(_url(product.id, attached[0].id))
    body = auth_client.delete(_url(product.id, attached[1].id)).json()

    assert body["reference_images"] == []
    assert body["is_generation_ready"] is False


def test_detach_keeps_the_media_asset(
    auth_client: Any, product: Any, attached: list[MediaAsset]
) -> None:
    auth_client.delete(_url(product.id, attached[0].id))

    assert MediaAsset.objects.filter(id=attached[0].id).exists()


def test_an_asset_not_attached_to_this_product_is_404(
    auth_client: Any, product: Any, workspace: Any, make_png_upload: Any
) -> None:
    loose = ingest_media(workspace=workspace, upload=make_png_upload())

    response = auth_client.delete(_url(product.id, loose.id))

    assert response.status_code == 404


def test_another_organizations_product_is_404(
    auth_client: Any, make_png_upload: Any, product: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    elsewhere = provision_workspace(stranger, name="Elsewhere")
    theirs = Product.objects.create(workspace=elsewhere, name="Theirs")
    [asset] = attach_reference_images(product=theirs, uploads=[make_png_upload()])

    response = auth_client.delete(_url(theirs.id, asset.id))

    assert response.status_code == 404
    assert theirs.reference_images.filter(id=asset.id).exists()


def test_a_sibling_workspaces_product_is_404(
    auth_client: Any, user: Any, workspace: Any, plans: Any, make_png_upload: Any, product: Any
) -> None:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    sibling = provision_extra_workspace(user=user, name="Sister Brand")
    theirs = Product.objects.create(workspace=sibling, name="Sister product")
    [asset] = attach_reference_images(product=theirs, uploads=[make_png_upload()])

    response = auth_client.delete(_url(theirs.id, asset.id), HTTP_X_WORKSPACE_ID=str(workspace.id))

    assert response.status_code == 404
    assert theirs.reference_images.filter(id=asset.id).exists()
