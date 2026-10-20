"""What the import knows how sure it is of (S1-02), the offers and verified
competitors it reads (S1-03), and what applying it writes (S1-04, S1-05)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rest_framework.test import APIClient

from brand.fetching import FakeSiteFetcher, set_override
from brand.models import SECTIONS, BrandCore
from brand.services import downstream, extract, imports
from categories.models import Category
from products.models import Product
from taste.models import TasteProfile
from trends.models import TrendSource

pytestmark = pytest.mark.django_db

IMPORTS = "/api/v1/brand/imports/"


@pytest.fixture(autouse=True)
def _food(db: None) -> None:
    Category.objects.get_or_create(name="Food & Drink", defaults={"slug": "food-drink"})


@pytest.fixture
def web() -> Any:
    fake = FakeSiteFetcher()
    set_override(fake)
    yield fake
    set_override(None)


class Rivals:
    """An inference that names one rival that exists, one that does not, and
    one whose site answers but under another name."""

    def infer(self, **_: Any) -> dict[str, Any]:
        return {
            "competitors": [
                {
                    "name": "Rival Olives",
                    "domain": "rival-olives.tn",
                    "why": "same oil",
                    "relationship": "inferred",
                },
                {
                    "name": "Ghost Brand",
                    "domain": "ghost-brand.tn",
                    "why": "?",
                    "relationship": "inferred",
                },
                {
                    "name": "Somebody Else",
                    "domain": "rival-olives.tn",
                    "why": "?",
                    "relationship": "inferred",
                },
                {"name": "No Domain Co", "domain": "", "why": "?", "relationship": "inferred"},
            ],
        }

    def infer_product(self, **_: Any) -> dict[str, Any]:
        return {}


def start(client: APIClient, url: str) -> int:
    response = client.post(IMPORTS, {"url": url}, format="json")
    assert response.status_code == 201, response.content
    return int(response.json()["id"])


def accept_all(client: APIClient, pk: int) -> None:
    for section in SECTIONS:
        client.post(
            f"{IMPORTS}{pk}/review/", {"section": section, "action": "accept"}, format="json"
        )


def run(client: APIClient, url: str) -> dict[str, Any]:
    pk = start(client, url)
    accept_all(client, pk)
    response = client.post(f"{IMPORTS}{pk}/apply/")
    assert response.status_code == 200, response.content
    return dict(response.json())


def pro(workspace: Any, plans: dict[str, Any]) -> None:
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])


# --- S1-02: confidence ----------------------------------------------------------------------
def test_each_found_section_says_how_sure_the_reading_is(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    pk = start(auth_client, "djerbaolive.tn")

    confidence = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]["confidence"]

    assert confidence["products"] in ("high", "medium")
    assert confidence["palette"] == "high"
    assert confidence["voice"] == "inferred"


def test_a_section_that_found_nothing_has_no_confidence_at_all(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    """Unknowns are omitted, not scored low."""
    pk = start(auth_client, "plainstudio.com")

    confidence = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]["confidence"]

    assert "products" not in confidence and "palette" not in confidence


def test_the_kit_stores_each_sections_confidence_and_a_hand_edit_is_confirmed(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    pk = start(auth_client, "djerbaolive.tn")
    accept_all(auth_client, pk)
    auth_client.post(
        f"{IMPORTS}{pk}/review/",
        {"section": "audience", "action": "edit", "value": {"summary": "Chefs.", "segments": []}},
        format="json",
    )
    auth_client.post(f"{IMPORTS}{pk}/apply/")

    sections = BrandCore.objects.get(workspace=workspace).sections

    assert sections["audience"]["confidence"] == "confirmed"
    assert sections["palette"]["confidence"] == "high"


# --- S1-03: offers and verified competitors -------------------------------------------------------
def test_offers_are_the_sites_own_words_with_their_page() -> None:
    page = extract.parse_page(
        "https://offerhouse.tn/",
        Path("brand/fixtures/sites/offerhouse.tn/index.html").read_text(encoding="utf-8"),
    )

    offers = extract.extract_offers([page])

    assert [(o["kind"], o["text"]) for o in offers] == [
        ("shipping", "Free delivery on every order over 80 TND"),
        ("code", "Use code HARVEST20 at checkout"),
    ]
    assert all(o["source"] == "https://offerhouse.tn/" for o in offers)


def test_a_page_with_no_offer_lists_none(web: Any) -> None:
    page = extract.parse_page(
        "https://plainstudio.com/", "<html><body><p>Hello there friend, welcome.</p></body></html>"
    )

    assert extract.extract_offers([page]) == []


def test_offers_arrive_in_the_products_section(auth_client: Any, workspace: Any, web: Any) -> None:
    pk = start(auth_client, "offerhouse.tn")

    offers = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]["sections"]["products"]["offers"]

    assert len(offers) == 2


def test_only_a_rival_whose_own_site_says_its_name_is_verified(
    auth_client: Any, workspace: Any, web: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports, "get_brand_inference", lambda: Rivals())
    pk = start(auth_client, "offerhouse.tn")

    result = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]
    rivals = {row["name"]: row for row in result["sections"]["competitors"]["items"]}

    assert rivals["Rival Olives"]["relationship"] == "verified"
    assert rivals["Rival Olives"]["source"] == "https://rival-olives.tn/"
    assert {s["platform"] for s in rivals["Rival Olives"]["socials"]} == {"instagram", "facebook"}
    assert rivals["Ghost Brand"]["relationship"] == "inferred"
    assert rivals["Somebody Else"]["relationship"] == "inferred", (
        "a site that answers under another name"
    )
    assert rivals["No Domain Co"]["relationship"] == "inferred"
    assert result["confidence"]["competitors"] == "mixed"


def test_a_competitor_pointing_back_at_the_brand_itself_is_not_verified(
    auth_client: Any, workspace: Any, web: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Self:
        def infer(self, **_: Any) -> dict[str, Any]:
            return {
                "competitors": [
                    {
                        "name": "Offer House",
                        "domain": "offerhouse.tn",
                        "why": "",
                        "relationship": "inferred",
                    }
                ]
            }

        def infer_product(self, **_: Any) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(imports, "get_brand_inference", lambda: Self())
    pk = start(auth_client, "offerhouse.tn")

    [row] = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]["sections"]["competitors"]["items"]

    assert row["relationship"] == "inferred"


# --- S1-04: what applying writes
def test_listed_products_become_product_drafts_within_the_plans_cap(
    auth_client: Any, workspace: Any, web: Any, plans: dict[str, Any]
) -> None:
    """The trial plan allows one product; the rest are reported, not lost."""
    body = run(auth_client, "djerbaolive.tn")

    assert len(body["wrote"]["products_created"]) == 1
    assert body["wrote"]["products_skipped"], "the cap is said out loud"
    assert Product.objects.filter(workspace=workspace).count() == 1


def test_a_paid_plan_gets_every_listed_product_and_none_twice(
    auth_client: Any, workspace: Any, web: Any, plans: dict[str, Any]
) -> None:
    pro(workspace, plans)

    first = run(auth_client, "djerbaolive.tn")
    created = len(first["wrote"]["products_created"])
    second = run(auth_client, "djerbaolive.tn")

    assert created == 4
    assert second["wrote"]["products_created"] == []
    assert Product.objects.filter(workspace=workspace).count() == 4


def test_a_product_the_workspace_already_has_is_left_alone(
    auth_client: Any, workspace: Any, web: Any, plans: dict[str, Any]
) -> None:
    pro(workspace, plans)
    pk = start(auth_client, "djerbaolive.tn")
    first_name = auth_client.get(f"{IMPORTS}{pk}/").json()["result"]["sections"]["products"][
        "items"
    ][0]["name"]
    existing = Product.objects.create(
        workspace=workspace, name=first_name.upper(), description="mine"
    )
    accept_all(auth_client, pk)

    body = auth_client.post(f"{IMPORTS}{pk}/apply/").json()

    assert first_name not in body["wrote"]["products_created"]
    existing.refresh_from_db()
    assert existing.description == "mine"


def test_the_voice_becomes_a_new_taste_version_and_is_active_when_nothing_was_written(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    body = run(auth_client, "djerbaolive.tn")

    assert body["wrote"]["taste_version"] == 1
    assert body["wrote"]["taste_activated"] is True
    profile = TasteProfile.objects.get(workspace=workspace, is_active=True)
    assert profile.voice["tone"] and profile.voice["source"] == "brand_import"


def test_a_profile_a_person_wrote_is_never_replaced_by_the_import(
    auth_client: Any, workspace: Any, web: Any, user: Any
) -> None:
    from taste.services import profiles

    mine = profiles.create_profile(
        workspace=workspace,
        created_by=user,
        voice={"tone": "dry and precise", "formality": "high"},
        hard_constraints={"banned_topics": ["rivals"]},
    )
    profiles.activate(mine)

    body = run(auth_client, "djerbaolive.tn")

    assert body["wrote"]["taste_version"] == 2
    assert body["wrote"]["taste_activated"] is False
    assert TasteProfile.objects.get(workspace=workspace, is_active=True).pk == mine.pk
    proposal = TasteProfile.objects.get(workspace=workspace, version=2)
    assert proposal.hard_constraints == {"banned_topics": ["rivals"]}, "carried over untouched"
    assert proposal.voice["formality"] == "high"
    assert proposal.is_active is False


def test_a_second_import_replaces_the_profile_the_first_one_wrote(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    run(auth_client, "djerbaolive.tn")

    body = run(auth_client, "nabeulceramics.com")

    assert body["wrote"]["taste_version"] == 2 and body["wrote"]["taste_activated"] is True
    assert TasteProfile.objects.filter(workspace=workspace, is_active=True).count() == 1


def test_verified_competitors_with_a_handle_become_trend_sources_and_guesses_do_not(
    auth_client: Any,
    workspace: Any,
    web: Any,
    plans: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pro(workspace, plans)
    workspace.category = Category.objects.get(name="Food & Drink")
    workspace.save(update_fields=["category"])
    monkeypatch.setattr(imports, "get_brand_inference", lambda: Rivals())

    body = run(auth_client, "offerhouse.tn")

    assert body["wrote"]["competitors_tracked"] == ["Rival Olives"]
    source = TrendSource.objects.get(workspace=workspace, kind="COMPETITOR")
    assert (source.platform, source.handle) == ("instagram", "rivalolives")


def test_a_verified_competitor_with_no_account_is_reported_not_guessed(workspace: Any) -> None:
    out = downstream.Downstream()

    downstream.track_competitors(
        workspace, [{"name": "Rival", "relationship": "verified", "socials": []}], out
    )

    assert out.competitors_tracked == []
    assert out.competitors_skipped == [
        {"name": "Rival", "reason": "Its website shows no account to watch."}
    ]


def test_a_section_kept_from_a_hand_edit_writes_nothing_again(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    first = start(auth_client, "djerbaolive.tn")
    accept_all(auth_client, first)
    auth_client.post(
        f"{IMPORTS}{first}/review/",
        {
            "section": "voice",
            "action": "edit",
            "value": {"preset": "WARM", "descriptors": ["calm"]},
        },
        format="json",
    )
    auth_client.post(f"{IMPORTS}{first}/apply/")
    versions = TasteProfile.objects.filter(workspace=workspace).count()

    body = run(auth_client, "djerbaolive.tn")

    assert "voice" in body["kept"]
    assert body["wrote"]["taste_version"] is None
    assert TasteProfile.objects.filter(workspace=workspace).count() == versions


# --- S1-05: the field-level difference
def test_the_first_import_has_nothing_to_differ_from(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    assert run(auth_client, "djerbaolive.tn")["changes"] == []


def test_a_reimport_reports_each_field_that_moved(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    run(auth_client, "djerbaolive.tn")

    changes = run(auth_client, "nabeulceramics.com")["changes"]

    kinds = {(c["section"], c["kind"]) for c in changes}
    assert ("products", "added") in kinds and ("products", "removed") in kinds
    assert ("palette", "changed") in kinds or ("palette", "added") in kinds
    assert all(set(c) == {"section", "field", "kind", "before", "after"} for c in changes)


def test_importing_the_same_site_again_reports_no_change(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    run(auth_client, "djerbaolive.tn")

    assert run(auth_client, "djerbaolive.tn")["changes"] == []


def test_a_kept_section_is_not_in_the_difference(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    first = start(auth_client, "djerbaolive.tn")
    accept_all(auth_client, first)
    auth_client.post(
        f"{IMPORTS}{first}/review/",
        {
            "section": "palette",
            "action": "edit",
            "value": {"colors": [{"role": "Primary", "hex": "#112233"}], "fonts": {}},
        },
        format="json",
    )
    auth_client.post(f"{IMPORTS}{first}/apply/")

    body = run(auth_client, "nabeulceramics.com")

    assert "palette" in body["kept"]
    assert all(c["section"] != "palette" for c in body["changes"])


def test_the_diff_is_pure_and_bounded() -> None:
    before: dict[str, Any] = {"products": {"value": {"items": []}}}
    after = {
        "products": {"value": {"items": [{"name": f"P{n}", "url": f"/p/{n}"} for n in range(80)]}}
    }

    changes = downstream.diff_sections(before, after)

    assert len(changes) == downstream.MAX_CHANGES
    assert before == {"products": {"value": {"items": []}}}
