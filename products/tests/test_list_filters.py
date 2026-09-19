"""`GET /products/` filters and per-product counts — the products page.

The page paginates (25 per page), so search and status filters run here rather
than in the browser: filtering only the rows already loaded would report "no
matches" for a product sitting on page two.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from content.models import Post, PostStatus
from products.models import AutopilotConfig, Product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PRODUCTS_URL = "/api/v1/products/"


def _make(workspace: Any, name: str, **fields: Any) -> Product:
    # Direct rows, not `create_product`: the plan cap would stop at one, and the
    # stored score/readiness are what the filters read, so setting them is the
    # point.
    return Product.objects.create(workspace=workspace, name=name, **fields)


def _names(response: Any) -> set[str]:
    assert response.status_code == 200, response.content
    return {p["name"] for p in response.json()["results"]}


@pytest.fixture
def catalogue(workspace: Any) -> dict[str, Product]:
    return {
        "serum": _make(
            workspace,
            "Lumina Serum",
            description="Hyaluronic glow",
            completeness_score=95,
            is_generation_ready=True,
            platforms=["instagram", "tiktok"],
        ),
        "mug": _make(
            workspace,
            "Ceramic Mug",
            description="Speckled clay",
            completeness_score=35,
            is_generation_ready=False,
            platforms=["pinterest"],
        ),
        "shirt": _make(
            workspace,
            "Linen Overshirt",
            completeness_score=70,
            is_generation_ready=True,
            platforms=["instagram"],
        ),
    }


# --- search ------------------------------------------------------------------
def test_q_matches_name_case_insensitively(auth_client: Any, catalogue: Any) -> None:
    assert _names(auth_client.get(PRODUCTS_URL, {"q": "serum"})) == {"Lumina Serum"}


def test_q_matches_description(auth_client: Any, catalogue: Any) -> None:
    assert _names(auth_client.get(PRODUCTS_URL, {"q": "CLAY"})) == {"Ceramic Mug"}


def test_blank_q_is_no_filter(auth_client: Any, catalogue: Any) -> None:
    assert len(_names(auth_client.get(PRODUCTS_URL, {"q": "  "}))) == 3


# --- status ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ready", {"Lumina Serum", "Linen Overshirt"}),
        ("needs_reference", {"Ceramic Mug"}),
        ("high_completeness", {"Lumina Serum"}),
    ],
)
def test_status_filters(auth_client: Any, catalogue: Any, status: str, expected: set[str]) -> None:
    assert _names(auth_client.get(PRODUCTS_URL, {"status": status})) == expected


def test_status_autopilot_on(auth_client: Any, catalogue: Any) -> None:
    AutopilotConfig.objects.create(product=catalogue["serum"], enabled=True)
    AutopilotConfig.objects.create(product=catalogue["mug"], enabled=False)

    assert _names(auth_client.get(PRODUCTS_URL, {"status": "autopilot_on"})) == {"Lumina Serum"}


def test_unknown_status_is_rejected_not_ignored(auth_client: Any, catalogue: Any) -> None:
    # A typo that returns everything is worse than one that 400s.
    response = auth_client.get(PRODUCTS_URL, {"status": "bogus"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_status"


# --- platform ----------------------------------------------------------------
def test_platform_filter(auth_client: Any, catalogue: Any) -> None:
    assert _names(auth_client.get(PRODUCTS_URL, {"platform": "instagram"})) == {
        "Lumina Serum",
        "Linen Overshirt",
    }


def test_unknown_platform_is_rejected_not_ignored(auth_client: Any, catalogue: Any) -> None:
    response = auth_client.get(PRODUCTS_URL, {"platform": "myspace"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_platform"


def test_filters_combine(auth_client: Any, catalogue: Any) -> None:
    response = auth_client.get(
        PRODUCTS_URL, {"platform": "instagram", "status": "high_completeness"}
    )
    assert _names(response) == {"Lumina Serum"}


def test_count_reflects_the_filter_not_the_page(auth_client: Any, catalogue: Any) -> None:
    body = auth_client.get(PRODUCTS_URL, {"status": "ready", "page_size": 1}).json()

    assert body["count"] == 2
    assert len(body["results"]) == 1
    assert body["next"] is not None


# --- counts ------------------------------------------------------------------
def test_counts_are_real_and_default_to_zero(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any
) -> None:
    serum = catalogue["serum"]
    AutopilotConfig.objects.create(product=serum, enabled=True)
    for status in (PostStatus.DRAFT, PostStatus.PUBLISHED, PostStatus.PUBLISHED):
        Post.objects.create(workspace=workspace, author=user, product=serum, status=status)

    rows = {p["name"]: p for p in auth_client.get(PRODUCTS_URL).json()["results"]}

    assert rows["Lumina Serum"]["post_count"] == 3
    assert rows["Lumina Serum"]["published_count"] == 2
    assert rows["Lumina Serum"]["autopilot_enabled"] is True
    # No autopilot row at all reads as off, not as missing.
    assert rows["Ceramic Mug"]["post_count"] == 0
    assert rows["Ceramic Mug"]["published_count"] == 0
    assert rows["Ceramic Mug"]["autopilot_enabled"] is False


def test_counts_on_detail_match_the_list(
    auth_client: Any, workspace: Any, user: Any, catalogue: Any
) -> None:
    serum = catalogue["serum"]
    Post.objects.create(workspace=workspace, author=user, product=serum, status=PostStatus.DRAFT)

    body = auth_client.get(f"{PRODUCTS_URL}{serum.id}/").json()

    assert body["post_count"] == 1
    assert body["published_count"] == 0


# --- tenancy -----------------------------------------------------------------
def test_search_never_reaches_another_organization(auth_client: Any, catalogue: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    _make(provision_workspace(stranger, name="Elsewhere"), "Lumina Serum Rival")

    assert _names(auth_client.get(PRODUCTS_URL, {"q": "lumina"})) == {"Lumina Serum"}


def test_search_never_reaches_a_sibling_workspace(
    auth_client: Any, user: Any, workspace: Any, catalogue: Any, plans: Any
) -> None:
    workspace.organization.plan = plans["advanced"]
    workspace.organization.save(update_fields=["plan"])
    sibling = provision_extra_workspace(user=user, name="Sister Brand")
    _make(sibling, "Lumina Serum Sister")

    response = auth_client.get(PRODUCTS_URL, {"q": "lumina"}, HTTP_X_WORKSPACE_ID=str(workspace.id))

    assert _names(response) == {"Lumina Serum"}
