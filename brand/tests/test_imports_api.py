"""The import's API: start, read, review, apply, skip — and who may see it.

Celery is eager in tests, so `POST /brand/imports/` returns the finished read
against the fake web (`brand/fixtures/sites/`)."""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from billing.models import FeatureFlag
from billing.services.flags import BRAND_IMPORT_S1
from brand.fetching import FakeSiteFetcher, set_override
from brand.models import SECTIONS, BrandCore, BrandImport, ImportStatus
from categories.models import Category
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

IMPORTS = "/api/v1/brand/imports/"
CORE = "/api/v1/brand/core/"


@pytest.fixture(autouse=True)
def _food(db: None) -> None:
    Category.objects.get_or_create(name="Food & Drink", defaults={"slug": "food-drink"})


@pytest.fixture
def web() -> Any:
    fake = FakeSiteFetcher()
    set_override(fake)
    yield fake
    set_override(None)


def start(client: APIClient, url: str = "djerbaolive.tn") -> Any:
    return client.post(IMPORTS, {"url": url}, format="json")


def accept_all(client: APIClient, pk: int) -> None:
    for section in SECTIONS:
        assert (
            client.post(
                f"{IMPORTS}{pk}/review/", {"section": section, "action": "accept"}, format="json"
            ).status_code
            == 200
        )


# --- reading ---------------------------------------------------------------------------
def test_an_import_reads_the_site_into_five_sections(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    response = start(auth_client, "https://www.DjerbaOlive.tn/some/page")

    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == ImportStatus.SUCCEEDED
    assert body["progress"] == 100
    assert body["url"] == "https://www.djerbaolive.tn/"
    assert body["domain"] == "djerbaolive.tn"
    sections = body["result"]["sections"]
    assert set(sections) == set(SECTIONS)
    assert len(sections["products"]["items"]) == 4
    assert {"role": "Primary", "hex": "#3F5A1E"}.items() <= sections["palette"]["colors"][0].items()
    assert sections["palette"]["fonts"]["display"]["name"] == "Playfair Display"
    assert all(c["relationship"] == "inferred" for c in sections["competitors"]["items"])
    assert body["result"]["identity"]["name"]["value"] == "Djerba Olive Co."
    assert body["result"]["market"]["category"]["name"] == "Food & Drink"
    assert body["pages_read"] >= 4
    # It read the shop, the about page and the stylesheet, and never left the site.
    assert any("/pages/about" in u for u in web.fetched)
    assert all("djerbaolive.tn" in u for u in web.fetched)


def test_a_quote_shown_is_one_the_site_actually_wrote(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    voice = start(auth_client).json()["result"]["sections"]["voice"]

    page = FakeSiteFetcher().fetch(voice["quote_source"])
    assert page is not None
    assert " ".join(voice["quote"].split()) in " ".join(page.text.split())


def test_an_unreachable_site_fails_with_nothing_invented(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    body = start(auth_client, "no-such-brand.example.com").json()

    assert body["status"] == ImportStatus.FAILED
    assert body["error"]
    assert body["result"] == {}


def test_a_bare_site_yields_empty_sections_not_made_up_ones(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    sections = start(auth_client, "plainstudio.com").json()["result"]["sections"]

    assert sections["products"]["items"] == []
    assert sections["palette"]["colors"] == []
    assert sections["competitors"]["items"] == []


@pytest.mark.parametrize(
    "url",
    ["not a website", "localhost", "http://printer.local/", "ftp://x.com", "https://u:p@x.com/"],
)
def test_an_invalid_website_is_400(auth_client: Any, workspace: Any, web: Any, url: str) -> None:
    response = start(auth_client, url)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_website"


def test_a_second_import_while_one_runs_is_409(auth_client: Any, workspace: Any, web: Any) -> None:
    BrandImport.objects.create(
        workspace=workspace, url="https://a.tn/", domain="a.tn", status=ImportStatus.RUNNING
    )

    response = start(auth_client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "import_running"


def test_latest_returns_the_newest_import_and_404s_when_there_is_none(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    assert auth_client.get(f"{IMPORTS}latest/").status_code == 404
    pk = start(auth_client).json()["id"]

    assert auth_client.get(f"{IMPORTS}latest/").json()["id"] == pk


# --- review ----------------------------------------------------------------------------
def test_review_accepts_and_edits_one_section_at_a_time(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    pk = start(auth_client).json()["id"]

    auth_client.post(
        f"{IMPORTS}{pk}/review/", {"section": "products", "action": "accept"}, format="json"
    )
    response = auth_client.post(
        f"{IMPORTS}{pk}/review/",
        {
            "section": "audience",
            "action": "edit",
            "value": {"summary": "Chefs and home cooks.", "segments": ["Chefs", " "]},
        },
        format="json",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review"] == {"products": "accepted", "audience": "edited"}
    assert body["edits"]["audience"] == {"summary": "Chefs and home cooks.", "segments": ["Chefs"]}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "section": "palette",
            "action": "edit",
            "value": {"colors": [{"role": "Primary", "hex": "green"}]},
        },
        {
            "section": "products",
            "action": "edit",
            "value": {"items": [{"name": "Oil", "price": {"amount": "lots"}}]},
        },
        {"section": "products", "action": "edit", "value": {"items": [{"name": ""}]}},
        {"section": "voice", "action": "edit", "value": {"preset": "SHOUTY"}},
    ],
)
def test_an_invalid_edit_is_400(
    auth_client: Any, workspace: Any, web: Any, payload: dict[str, Any]
) -> None:
    pk = start(auth_client).json()["id"]

    response = auth_client.post(f"{IMPORTS}{pk}/review/", payload, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_section"


def test_a_malformed_review_is_a_validation_error(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    pk = start(auth_client).json()["id"]
    assert start(auth_client, "").status_code == 400

    for payload in (
        {"section": "pricing", "action": "accept"},
        {"section": "voice", "action": "delete"},
        {"section": "competitors", "action": "edit", "value": "Zitouna"},
    ):
        assert auth_client.post(f"{IMPORTS}{pk}/review/", payload, format="json").status_code == 400


def test_reviewing_a_failed_import_is_409(auth_client: Any, workspace: Any, web: Any) -> None:
    pk = start(auth_client, "no-such-brand.example.com").json()["id"]

    response = auth_client.post(
        f"{IMPORTS}{pk}/review/", {"section": "voice", "action": "accept"}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "import_not_ready"


# --- apply ------------------------------------------------------------------------------
def test_apply_needs_every_section_reviewed(auth_client: Any, workspace: Any, web: Any) -> None:
    pk = start(auth_client).json()["id"]
    auth_client.post(
        f"{IMPORTS}{pk}/review/", {"section": "voice", "action": "accept"}, format="json"
    )

    response = auth_client.post(f"{IMPORTS}{pk}/apply/")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "review_incomplete"
    assert set(error["detail"]["missing"]) == {"products", "audience", "competitors", "palette"}


def test_apply_writes_a_brand_core_and_fills_only_blank_fields(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    workspace.description = "Our own words."
    workspace.save(update_fields=["description"])
    pk = start(auth_client).json()["id"]
    accept_all(auth_client, pk)

    response = auth_client.post(f"{IMPORTS}{pk}/apply/")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["core"]["version"] == 1
    assert body["kept"] == []
    assert "description" not in body["filled"]
    workspace.refresh_from_db()
    assert workspace.description == "Our own words."
    assert workspace.website == "https://djerbaolive.tn/"
    assert workspace.timezone == "Africa/Tunis"
    assert workspace.regions == ["TN"]
    assert workspace.platforms == ["instagram", "linkedin"]
    assert workspace.category.name == "Food & Drink"
    assert BrandImport.objects.get(pk=pk).status == ImportStatus.APPLIED
    assert auth_client.get(CORE).json()["sections"]["products"]["origin"] == "site"


def test_a_reimport_keeps_what_the_user_edited(auth_client: Any, workspace: Any, web: Any) -> None:
    first = start(auth_client).json()["id"]
    accept_all(auth_client, first)
    auth_client.post(
        f"{IMPORTS}{first}/review/",
        {"section": "audience", "action": "edit", "value": {"summary": "Chefs.", "segments": []}},
        format="json",
    )
    auth_client.post(f"{IMPORTS}{first}/apply/")

    second = start(auth_client).json()["id"]
    accept_all(auth_client, second)
    body = auth_client.post(f"{IMPORTS}{second}/apply/").json()

    assert body["core"]["version"] == 2
    assert body["kept"] == ["audience"]
    assert body["core"]["sections"]["audience"] == {
        "value": {"summary": "Chefs.", "segments": []},
        "origin": "you",
        "source": "",
    }
    assert BrandCore.objects.filter(workspace=workspace).count() == 2


def test_applying_twice_is_409(auth_client: Any, workspace: Any, web: Any) -> None:
    pk = start(auth_client).json()["id"]
    accept_all(auth_client, pk)
    auth_client.post(f"{IMPORTS}{pk}/apply/")

    assert auth_client.post(f"{IMPORTS}{pk}/apply/").json()["error"]["code"] == "import_not_ready"


def test_core_is_404_before_anything_is_applied(auth_client: Any, workspace: Any) -> None:
    assert auth_client.get(CORE).status_code == 404


# --- skip -------------------------------------------------------------------------------
def test_skip_is_recorded_once(auth_client: Any, workspace: Any) -> None:
    first = auth_client.post(f"{IMPORTS}skip/").json()
    second = auth_client.post(f"{IMPORTS}skip/").json()

    assert first["status"] == ImportStatus.SKIPPED
    assert first["id"] == second["id"]


# --- flags and tenancy --------------------------------------------------------------------
def test_flag_off_hides_every_endpoint(auth_client: Any, workspace: Any, web: Any) -> None:
    pk = start(auth_client).json()["id"]
    FeatureFlag.objects.create(organization=None, key=BRAND_IMPORT_S1, enabled=False)

    assert start(auth_client).status_code == 404
    assert auth_client.get(f"{IMPORTS}{pk}/").status_code == 404
    assert auth_client.get(CORE).status_code == 404
    assert auth_client.post(f"{IMPORTS}skip/").status_code == 404


def test_another_organizations_import_is_404(
    auth_client: Any, workspace: Any, other_user: Any, web: Any
) -> None:
    theirs_ws = provision_workspace(other_user, name="Theirs")
    theirs = BrandImport.objects.create(
        workspace=theirs_ws, url="https://t.tn/", domain="t.tn", status=ImportStatus.SUCCEEDED
    )

    assert auth_client.get(f"{IMPORTS}{theirs.pk}/").status_code == 404
    assert (
        auth_client.post(
            f"{IMPORTS}{theirs.pk}/review/", {"section": "voice", "action": "accept"}, format="json"
        ).status_code
        == 404
    )
    assert auth_client.post(f"{IMPORTS}{theirs.pk}/apply/").status_code == 404


def test_a_sibling_workspaces_import_is_404(
    auth_client: Any, workspace: Any, user: Any, web: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    theirs = BrandImport.objects.create(
        workspace=sibling, url="https://t.tn/", domain="t.tn", status=ImportStatus.SUCCEEDED
    )
    header = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}

    assert auth_client.get(f"{IMPORTS}{theirs.pk}/", **header).status_code == 404
    assert auth_client.post(f"{IMPORTS}{theirs.pk}/apply/", **header).status_code == 404
    # …and the sibling's own context sees it.
    assert (
        auth_client.get(f"{IMPORTS}{theirs.pk}/", HTTP_X_WORKSPACE_ID=str(sibling.pk)).status_code
        == 200
    )
