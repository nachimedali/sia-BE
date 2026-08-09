"""Product endpoints (design.md §7)."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.django_db

PRODUCTS_URL = "/api/v1/products/"


def _detail_url(product_id: int) -> str:
    return f"{PRODUCTS_URL}{product_id}/"


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------
def test_create_product_assigns_workspace_and_defaults(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(PRODUCTS_URL, {"name": "Aurora Ceramic Mug"}, format="json")
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "Aurora Ceramic Mug"
    assert body["is_generation_ready"] is False
    assert body["reference_images"] == []
    assert body["completeness_score"] > 0  # motion_reference is trivially satisfied


def test_list_products_is_workspace_scoped(auth_client: Any, product: Any) -> None:
    response = auth_client.get(PRODUCTS_URL)
    assert response.status_code == 200
    names = [p["name"] for p in response.json()["results"]]
    assert names == [product.name]


def test_retrieve_product(auth_client: Any, product: Any) -> None:
    response = auth_client.get(_detail_url(product.id))
    assert response.status_code == 200
    assert response.json()["id"] == product.id


def test_patch_product_updates_fields_and_recomputes_completeness(
    auth_client: Any, product: Any
) -> None:
    before = auth_client.get(_detail_url(product.id)).json()["completeness_score"]

    response = auth_client.patch(
        _detail_url(product.id), {"description": "Hand-glazed 12oz mug."}, format="json"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Hand-glazed 12oz mug."
    assert body["completeness_score"] > before


def test_product_has_no_delete_endpoint(auth_client: Any, product: Any) -> None:
    response = auth_client.delete(_detail_url(product.id))
    assert response.status_code == 405


# -----------------------------------------------------------------------------
# Reference images (I7)
# -----------------------------------------------------------------------------
def test_attach_reference_image_flips_generation_ready(
    auth_client: Any, product: Any, make_png_upload: Any
) -> None:
    response = auth_client.post(
        f"{_detail_url(product.id)}reference-images/",
        {"files": [make_png_upload()]},
        format="multipart",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_generation_ready"] is True
    assert len(body["reference_images"]) == 1


def test_attach_reference_image_with_no_files_is_rejected(auth_client: Any, product: Any) -> None:
    response = auth_client.post(
        f"{_detail_url(product.id)}reference-images/", {}, format="multipart"
    )
    assert response.status_code == 400


# -----------------------------------------------------------------------------
# Completeness (I7)
# -----------------------------------------------------------------------------
def test_completeness_endpoint_lists_missing_inputs_with_impact(
    auth_client: Any, product: Any
) -> None:
    response = auth_client.get(f"{_detail_url(product.id)}completeness/")
    assert response.status_code == 200
    body = response.json()
    assert body["is_generation_ready"] is False
    assert any(item["key"] == "references" for item in body["missing"])
    assert all(item["impact"] > 0 for item in body["missing"])


# -----------------------------------------------------------------------------
# Quota (I8)
# -----------------------------------------------------------------------------
def test_product_quota_enforced_per_plan(auth_client: Any, workspace: Any, plans: Any) -> None:
    # Free plan's max_products is 1 (billing/management/commands/seed_plans.py).
    first = auth_client.post(PRODUCTS_URL, {"name": "First product"}, format="json")
    assert first.status_code == 201

    second = auth_client.post(PRODUCTS_URL, {"name": "Second product"}, format="json")
    assert second.status_code == 402
    body = second.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert "upgrade" in body["error"]
