"""Importing one product page into a new-product brief (S1, the product half)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai.creative_seed import CATALOG
from ai.models import CreativeOption
from billing.models import FeatureFlag
from billing.services.flags import BRAND_IMPORT_S1
from brand.fetching import FakeSiteFetcher, set_override
from brand.models import ImportStatus, ProductImport
from brand.services.extract import parse_page
from brand.services.product_imports import normalise_product_url
from brand.services.product_page import extract_product, short_from
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

IMPORTS = "/api/v1/brand/product-imports/"
CLAIMS = [
    ("natural", ["100% natural", "100% naturelle"]),
    ("handmade", ["handmade", "peinte à la main"]),
    ("made-in-tunisia", ["made in tunisia", "produit de tunisie"]),
    ("organic", ["organic", "biologique"]),
]


@pytest.fixture
def web() -> Any:
    fake = FakeSiteFetcher()
    set_override(fake)
    yield fake
    set_override(None)


@pytest.fixture
def claims(db: None) -> None:
    for position, (key, phrases) in enumerate(CLAIMS):
        CreativeOption.objects.create(
            kind="claim", key=key, label=key, sort_order=position, metadata={"phrases": phrases}
        )


def _page(url: str) -> Any:
    fetched = FakeSiteFetcher().fetch(url)
    assert fetched is not None and fetched.ok
    return parse_page(fetched.url, fetched.text)


def _json(url: str) -> Any:
    fetched = FakeSiteFetcher().fetch(url)
    assert fetched is not None and fetched.ok
    return json.loads(fetched.text)


def value(result: dict[str, Any], field: str) -> Any:
    return result[field]["value"]


# --- extraction ------------------------------------------------------------------------
def test_a_shopify_product_is_read_from_its_json_with_the_pages_currency() -> None:
    url = "https://djerbaolive.tn/products/first-harvest"
    result = extract_product(
        _page(url), shopify=(f"{url}.json", _json(f"{url}.json")), claim_rows=CLAIMS
    )

    assert result["via"] == "shopify"
    assert value(result, "name") == "First harvest EVOO 500 ml"
    assert value(result, "brand") == "Djerba Olive Co."
    assert value(result, "sku") == "DOC-EVOO-500"
    # Shopify's JSON states no currency; the page's own tags do.
    assert value(result, "price") == {"amount": "38.5", "currency": "TND"}
    assert value(result, "features") == [
        "Cold-pressed",
        "First harvest",
        "Numbered bottles",
        "0.2% acidity",
    ]
    assert value(result, "short").startswith("Cold-pressed within hours of picking")
    assert len(value(result, "short")) <= 160
    assert value(result, "images") == [
        "https://djerbaolive.tn/cdn/first-harvest.jpg",
        "https://djerbaolive.tn/cdn/first-harvest-side.jpg",
    ]
    assert result["sku"]["source"] == f"{url}.json"
    assert value(result, "language") == "en"


def test_claims_come_with_the_sentence_that_states_them_and_only_when_stated() -> None:
    url = "https://djerbaolive.tn/products/first-harvest"
    claims = value(
        extract_product(
            _page(url), shopify=(f"{url}.json", _json(f"{url}.json")), claim_rows=CLAIMS
        ),
        "claims",
    )

    keys = [claim["key"] for claim in claims]
    assert keys == ["natural", "made-in-tunisia"]
    assert "organic" not in keys and "handmade" not in keys
    assert all(claim["quote"] and len(claim["quote"]) <= 160 for claim in claims)
    assert "Produit de Tunisie" in claims[1]["quote"]


def test_a_jsonld_product_gives_sku_brand_price_and_absolute_images() -> None:
    result = extract_product(
        _page("https://nabeulceramics.com/product/dinner-plate"), claim_rows=CLAIMS
    )

    assert result["via"] == "json-ld"
    assert value(result, "sku") == "NC-PLATE-28"
    assert value(result, "brand") == "Nabeul Ceramics"
    assert value(result, "price") == {"amount": "42", "currency": "TND"}
    assert value(result, "images") == [
        "https://nabeulceramics.com/img/plate.jpg",
        "https://nabeulceramics.com/img/plate-detail.jpg",
    ]
    assert value(result, "features") == ["Peinte à la main", "Émail sans plomb", "Pièce unique"]
    assert [c["key"] for c in value(result, "claims")] == ["handmade"]
    assert value(result, "language") == "fr"


def test_an_opengraph_page_keeps_its_own_currency() -> None:
    result = extract_product(_page("https://plainstudio.com/shop/linen-tote"), claim_rows=CLAIMS)

    assert result["via"] == "opengraph"
    assert value(result, "name") == "Linen tote"
    assert value(result, "price") == {"amount": "19.9", "currency": "EUR"}
    assert value(result, "images") == ["https://plainstudio.com/media/tote.png"]
    assert "claims" not in result
    assert "sku" not in result


def test_navigation_and_logos_are_not_features_or_photos() -> None:
    # The page alone, without its shop JSON: the nav menu is a list too.
    result = extract_product(_page("https://djerbaolive.tn/products/first-harvest"), claim_rows=[])

    assert "features" not in result
    assert "https://djerbaolive.tn/assets/logo.png" not in value(result, "images")


def test_a_short_description_is_whole_sentences() -> None:
    text = "One. " + "A sentence that keeps going for quite a while to fill the space. " * 4
    short = short_from(text)
    assert short.endswith(".") and len(short) <= 160


def test_every_seeded_claim_carries_phrases_to_match() -> None:
    assert all(row["metadata"].get("phrases") for row in CATALOG["claim"])


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        (
            "djerbaolive.tn/products/first-harvest?variant=2#reviews",
            "https://djerbaolive.tn/products/first-harvest?variant=2",
        ),
        ("HTTP://Shop.Example.com/p/1", "http://shop.example.com/p/1"),
    ],
)
def test_a_product_url_keeps_its_path_and_query(raw: str, url: str) -> None:
    assert normalise_product_url(raw)[0] == url


# --- API -------------------------------------------------------------------------------
@pytest.mark.django_db
def test_importing_a_page_returns_the_brief_and_creates_no_product(
    auth_client: Any, workspace: Any, web: Any, claims: None
) -> None:
    from products.models import Product

    response = auth_client.post(
        IMPORTS, {"url": "djerbaolive.tn/products/first-harvest"}, format="json"
    )

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == ImportStatus.SUCCEEDED
    assert body["stage"] == "DONE" and body["progress"] == 100
    assert body["result"]["name"]["value"] == "First harvest EVOO 500 ml"
    assert [c["key"] for c in body["result"]["claims"]["value"]] == ["natural", "made-in-tunisia"]
    assert body["pages"] == [
        "https://djerbaolive.tn/products/first-harvest",
        "https://djerbaolive.tn/products/first-harvest.json",
    ]
    assert Product.objects.count() == 0
    assert auth_client.get(f"{IMPORTS}{body['id']}/").json()["id"] == body["id"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url", ["not a link", "localhost/products/x", "ftp://shop.tn/p", "https://u:p@shop.tn/p"]
)
def test_an_invalid_link_is_400(auth_client: Any, workspace: Any, web: Any, url: str) -> None:
    response = auth_client.post(IMPORTS, {"url": url}, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_product_url"


@pytest.mark.django_db
def test_an_unreachable_page_fails_with_nothing_found(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    body = auth_client.post(
        IMPORTS, {"url": "https://gone.example.com/products/x"}, format="json"
    ).json()

    assert body["status"] == ImportStatus.FAILED
    assert body["error"].startswith("We couldn't read that page")
    assert body["result"] == {}


@pytest.mark.django_db
def test_a_home_page_is_not_a_product_page(auth_client: Any, workspace: Any, web: Any) -> None:
    body = auth_client.post(IMPORTS, {"url": "https://plainstudio.com/"}, format="json").json()

    assert body["status"] == ImportStatus.FAILED
    assert "does not look like a product page" in body["error"]


@pytest.mark.django_db
def test_a_found_photo_is_served_for_the_forms_upload(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    pk = auth_client.post(
        IMPORTS, {"url": "https://djerbaolive.tn/products/first-harvest"}, format="json"
    ).json()["id"]

    response = auth_client.get(f"{IMPORTS}{pk}/images/1/")

    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"
    assert auth_client.get(f"{IMPORTS}{pk}/images/9/").status_code == 404


@pytest.mark.django_db
def test_a_photo_the_shop_no_longer_serves_is_422(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    run = ProductImport.objects.create(
        workspace=workspace,
        url="https://djerbaolive.tn/products/x",
        status=ImportStatus.SUCCEEDED,
        result={"images": {"value": ["https://djerbaolive.tn/cdn/missing.jpg"], "source": "x"}},
    )

    response = auth_client.get(f"{IMPORTS}{run.pk}/images/0/")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "image_unavailable"


@pytest.mark.django_db
def test_the_photo_endpoint_only_serves_what_the_import_found(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    run = ProductImport.objects.create(
        workspace=workspace,
        url="https://djerbaolive.tn/products/x",
        status=ImportStatus.FAILED,
        result={},
    )

    assert auth_client.get(f"{IMPORTS}{run.pk}/images/0/").status_code == 404


@pytest.mark.django_db
def test_flag_off_hides_product_import(auth_client: Any, workspace: Any, web: Any) -> None:
    FeatureFlag.objects.create(organization=None, key=BRAND_IMPORT_S1, enabled=False)

    assert (
        auth_client.post(
            IMPORTS, {"url": "https://djerbaolive.tn/products/first-harvest"}, format="json"
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_another_organizations_import_and_photos_are_404(
    auth_client: Any, workspace: Any, other_user: Any, web: Any
) -> None:
    theirs = ProductImport.objects.create(
        workspace=provision_workspace(other_user, name="Theirs"),
        url="https://djerbaolive.tn/products/first-harvest",
        status=ImportStatus.SUCCEEDED,
        result={
            "images": {"value": ["https://djerbaolive.tn/cdn/first-harvest.jpg"], "source": "x"}
        },
    )

    assert auth_client.get(f"{IMPORTS}{theirs.pk}/").status_code == 404
    assert auth_client.get(f"{IMPORTS}{theirs.pk}/images/0/").status_code == 404


@pytest.mark.django_db
def test_a_sibling_workspaces_import_is_404(
    auth_client: Any, workspace: Any, user: Any, web: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    theirs = ProductImport.objects.create(
        workspace=sibling,
        url="https://djerbaolive.tn/products/first-harvest",
        status=ImportStatus.SUCCEEDED,
    )

    assert (
        auth_client.get(f"{IMPORTS}{theirs.pk}/", HTTP_X_WORKSPACE_ID=str(workspace.pk)).status_code
        == 404
    )
    assert (
        auth_client.get(f"{IMPORTS}{theirs.pk}/", HTTP_X_WORKSPACE_ID=str(sibling.pk)).status_code
        == 200
    )
