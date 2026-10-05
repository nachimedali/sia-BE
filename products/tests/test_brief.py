"""The product brief (`products.brief`): what `/app/products/new` collects.

Voice and rules here are *overrides* on the workspace's taste (L-1 / C-08) —
these tests pin the three ways they reach the system: validated against the
live catalog, told to the model, and screened where a text check can decide.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from products import brief
from products.models import HashtagStyle
from products.services.products import attach_reference_images, create_product
from taste.services.profiles import constraints_for

pytestmark = pytest.mark.django_db

URL = "/api/v1/products/"


@pytest.fixture(autouse=True)
def _catalog() -> None:
    call_command("seed_creative_options", verbosity=0)


@pytest.fixture
def other_workspace(plans: Any) -> Any:
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    owner = get_user_model().objects.create_user(email="other-owner@example.com", password="x")
    return provision_workspace(owner, name="Someone Else's Workspace")


def _detail(pk: int) -> str:
    return f"{URL}{pk}/"


FULL: dict[str, Any] = {
    "name": "Extra virgin olive oil 500 ml",
    "sku": "DOC-EVOO-500",
    "price": "38.500",
    "product_url": "https://example.com/oil",
    "short_description": "Cold-pressed first-harvest oil from Djerba groves, bottled when pressed.",
    "description": "Origin, process and how people use it.",
    "features": ["Cold-pressed", "Small batch"],
    "audience": ["home-cooks", "gift-buyers"],
    "languages": ["fr", "en"],
    "tone": {"formal": 35, "bold": 35, "modern": 25, "poetic": 70},
    "tone_preset": "artisan",
    "use_words": ["first harvest"],
    "avoid_words": ["cheap", "best ever"],
    "hashtags_style": "MODERATE",
    "caption_length": "medium",
    "scenes": ["beach-shoreline", "seamless-studio"],
    "lights": ["golden-hour"],
    "people": "hands",
    "brand_colors": ["#4a36a0", "#23918E"],
    "aspects": ["4-5"],
    "must_include": ["Logo visible"],
    "restrictions": ["Alcohol"],
    "ramadan_quiet_hours": True,
    "no_children": True,
    "mention_price": True,
    "approval_mode": "manager",
    "legal_mention": "Store away from heat and light.",
    "rules_reviewed": True,
    "photo_policy": {"bg_remove": True, "restage": True, "unaltered": True, "logo_legible": True},
    "platforms": ["instagram", "facebook"],
    "platform_plan": {
        "instagram": {"formats": ["FEED", "REEL"], "per_week": 4},
        "facebook": {"formats": ["FEED"], "per_week": 2},
    },
    "campaign_goal": "sales",
    "campaign_starts": "2026-11-01",
    "campaign_ends": "2026-12-01",
    "brand_hashtags": ["#MadeInTunisia", "djerba"],
    "ctas": ["shop-now"],
}


# --- happy path --------------------------------------------------------------
def test_the_whole_brief_round_trips(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(URL, FULL, format="json")

    assert response.status_code == 201, response.json()
    body = response.json()
    for key in ("sku", "features", "audience", "scenes", "people", "platform_plan", "tone"):
        assert body[key] == FULL[key]
    assert body["brand_colors"] == ["#4A36A0", "#23918E"]  # normalised
    assert body["brand_hashtags"] == ["#MadeInTunisia", "#djerba"]
    assert body["price"] == "38.500"
    assert body["price_currency"] == "TND"
    assert body["rules_reviewed_at"] is not None
    assert "rules_reviewed" not in body  # a checkbox in, a timestamp out


def test_unticking_the_reviewed_box_clears_the_timestamp(auth_client: Any, product: Any) -> None:
    auth_client.patch(_detail(product.id), {"rules_reviewed": True}, format="json")
    cleared = auth_client.patch(_detail(product.id), {"rules_reviewed": False}, format="json")
    assert cleared.json()["rules_reviewed_at"] is None


def test_a_patch_that_omits_the_brief_leaves_it_alone(auth_client: Any, workspace: Any) -> None:
    created = auth_client.post(URL, FULL, format="json").json()
    auth_client.patch(_detail(created["id"]), {"name": "Renamed"}, format="json")
    after = auth_client.get(_detail(created["id"])).json()
    assert after["name"] == "Renamed"
    assert after["features"] == FULL["features"]
    assert after["platform_plan"] == FULL["platform_plan"]


# --- every emittable validation error ---------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audience", ["martians"]),
        ("scenes", ["atlantis"]),
        ("lights", ["disco"]),
        ("aspects", ["7-3"]),
        ("languages", ["dj"]),  # Derja is not on the menu (L-6)
        ("languages", ["ar"]),
        ("people", "crowd"),
        ("tone_preset", "wild"),
        ("tone", {"formal": 101}),
        ("tone", {"swagger": 10}),
        ("brand_colors", ["purple"]),
        ("brand_colors", ["#111111"] * 5),
        ("photo_policy", {"sparkle": True}),
        ("claims", [{"key": "miracle", "proof_media": None}]),
        ("platform_plan", {"instagram": {"formats": ["FEED"], "per_week": 3}}),  # not selected
        ("brand_hashtags", ["#bad tag!"]),
        ("features", [f"feature {i}" for i in range(16)]),
        ("features", "not a list"),
        ("price", "-1"),
    ],
)
def test_an_unknown_or_malformed_value_is_a_400_naming_the_field(
    auth_client: Any, product: Any, field: str, value: Any
) -> None:
    response = auth_client.patch(_detail(product.id), {field: value}, format="json")
    assert response.status_code == 400, (field, response.json())
    assert field in str(response.json()), response.json()


def test_a_platform_cannot_be_given_a_format_it_does_not_offer(
    auth_client: Any, product: Any
) -> None:
    response = auth_client.patch(
        _detail(product.id),
        {
            "platforms": ["instagram"],
            "platform_plan": {"instagram": {"formats": ["PDF_CAROUSEL"], "per_week": 2}},
        },
        format="json",
    )
    assert response.status_code == 400


def test_posts_per_week_is_bounded(auth_client: Any, product: Any) -> None:
    response = auth_client.patch(
        _detail(product.id),
        {
            "platforms": ["instagram"],
            "platform_plan": {"instagram": {"formats": ["FEED"], "per_week": 99}},
        },
        format="json",
    )
    assert response.status_code == 400


def test_the_campaign_cannot_end_before_it_starts(auth_client: Any, product: Any) -> None:
    response = auth_client.patch(
        _detail(product.id),
        {"campaign_starts": "2026-12-01", "campaign_ends": "2026-11-01"},
        format="json",
    )
    assert response.status_code == 400
    assert "campaign_ends" in response.json()["error"]["detail"]["fields"]


def test_a_retired_catalog_row_is_refused_not_ignored(auth_client: Any, product: Any) -> None:
    from ai.models import CreativeOption

    CreativeOption.objects.filter(kind="scene", key="desert-dunes").update(is_active=False)
    response = auth_client.patch(_detail(product.id), {"scenes": ["desert-dunes"]}, format="json")
    assert response.status_code == 400


def test_reference_tags_cannot_be_set_before_there_are_photos(
    auth_client: Any, workspace: Any
) -> None:
    response = auth_client.post(
        URL, {"name": "Mug", "reference_tags": {"1": "front"}}, format="json"
    )
    assert response.status_code == 400


# --- tenancy -----------------------------------------------------------------
def test_another_workspaces_proof_reads_as_missing(
    auth_client: Any, product: Any, other_workspace: Any, make_png_upload: Any
) -> None:
    from content.services.media import ingest_media

    foreign = ingest_media(workspace=other_workspace, upload=make_png_upload())
    response = auth_client.patch(
        _detail(product.id),
        {"claims": [{"key": "organic", "proof_media": foreign.pk}]},
        format="json",
    )
    assert response.status_code == 400
    assert "not found" in str(response.json())


def test_another_workspaces_product_is_a_404_for_every_brief_write(
    auth_client: Any, workspace: Any, other_workspace: Any
) -> None:
    theirs = create_product(workspace=other_workspace, name="Not yours")
    assert (
        auth_client.patch(_detail(theirs.pk), {"features": ["x"]}, format="json").status_code == 404
    )
    assert auth_client.get(_detail(theirs.pk)).status_code == 404


# --- reference photo tagging -------------------------------------------------
def test_photos_are_tagged_as_they_are_uploaded(
    auth_client: Any, product: Any, make_png_upload: Any
) -> None:
    response = auth_client.post(
        f"{URL}{product.id}/reference-images/",
        {
            "files": [make_png_upload("a.png"), make_png_upload("b.png")],
            "tags": ["front", "detail"],
        },
        format="multipart",
    )
    assert response.status_code == 200, response.json()
    tags = response.json()["reference_tags"]
    ids = [str(img["id"]) for img in response.json()["reference_images"]]
    assert sorted(tags.values()) == ["detail", "front"]
    assert set(tags) == set(ids)


def test_an_unknown_shot_type_refuses_the_upload(
    auth_client: Any, product: Any, make_png_upload: Any
) -> None:
    response = auth_client.post(
        f"{URL}{product.id}/reference-images/",
        {"files": [make_png_upload()], "tags": ["selfie"]},
        format="multipart",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_shot_tag"
    product.refresh_from_db()
    assert product.reference_images.count() == 0  # nothing half-attached


def test_detaching_a_photo_drops_its_tag(
    auth_client: Any, product: Any, make_png_upload: Any
) -> None:
    (asset,) = attach_reference_images(
        product=product, uploads=[make_png_upload()], shot_tags=["front"]
    )
    auth_client.delete(f"{URL}{product.id}/reference-images/{asset.pk}/")
    product.refresh_from_db()
    assert product.reference_tags == {}


# --- what the model is told --------------------------------------------------
def test_the_prompt_carries_the_brief_and_only_the_brief(workspace: Any) -> None:
    product = create_product(
        workspace=workspace,
        name="Oil",
        short_description="Cold-pressed.",
        features=["Small batch"],
        tone={"formal": 10, "bold": 90, "modern": 50, "poetic": 50},
        use_words=["first harvest"],
        avoid_words=["cheap"],
        hashtags_style=HashtagStyle.NONE,
        no_children=True,
        legal_mention="Keep away from light.",
    )
    text = "\n".join(brief.prompt_lines(product))

    assert "Small batch" in text
    assert "playful" in text and "bold" in text  # the stops, as words
    assert '"cheap"' in text
    assert "no hashtags" in text.lower()
    assert "no children" in text.lower()
    assert "Keep away from light." in text
    assert "Audience" not in text  # unset fields say nothing


def test_an_empty_brief_adds_no_lines(workspace: Any) -> None:
    assert brief.prompt_lines(create_product(workspace=workspace, name="Plain")) == []


def test_only_a_claim_with_proof_attached_may_be_made(workspace: Any, make_png_upload: Any) -> None:
    from content.services.media import ingest_media

    proof = ingest_media(workspace=workspace, upload=make_png_upload())
    product = create_product(
        workspace=workspace,
        name="Oil",
        claims=[
            {"key": "organic", "proof_media": proof.pk},
            {"key": "handmade", "proof_media": None},
        ],
    )
    text = "\n".join(brief.prompt_lines(product))

    assert "Organic" in text
    assert "Hand-made" not in text  # ticked but never backed
    assert "Make no claim" in text


def test_the_product_prompt_reaches_the_text_and_image_assemblers(workspace: Any) -> None:
    from ai.services.prompting import assemble_image_prompt, assemble_text_prompt

    product = create_product(workspace=workspace, name="Oil", features=["Small batch"])
    assert (
        "Small batch" in assemble_text_prompt(idea="x", workspace=workspace, product=product).user
    )
    assert (
        "Small batch" in assemble_image_prompt(idea="x", workspace=workspace, product=product).user
    )


# --- what screening enforces -------------------------------------------------
def test_banned_words_and_no_hashtags_narrow_the_brand_policy(workspace: Any) -> None:
    product = create_product(
        workspace=workspace,
        name="Oil",
        avoid_words=["cheap"],
        restrictions=["Alcohol"],
        hashtags_style=HashtagStyle.NONE,
    )
    merged = constraints_for(workspace, product)

    assert merged["banned_phrases"] == ["cheap"]
    assert merged["banned_topics"] == ["Alcohol"]
    assert merged["max_hashtags"] == 0


def test_a_product_cannot_loosen_a_brand_rule(workspace: Any, plans: Any) -> None:
    from taste.services.profiles import activate, create_profile

    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan"])

    activate(create_profile(workspace=workspace, hard_constraints={"max_hashtags": 2}))
    product = create_product(workspace=workspace, name="Oil", hashtags_style=HashtagStyle.NONE)
    assert constraints_for(workspace, product)["max_hashtags"] == 0  # stricter wins

    loose = create_product(workspace=workspace, name="Loose", hashtags_style=HashtagStyle.HEAVY)
    assert constraints_for(workspace, loose)["max_hashtags"] == 2  # brand's own, untouched


# --- the completeness definitions endpoint ----------------------------------
def test_the_checks_are_served_as_data(auth_client: Any) -> None:
    response = auth_client.get(f"{URL}completeness-checks/")
    assert response.status_code == 200
    rows = response.json()
    assert sum(r["weight"] for r in rows) == 100
    assert {"key", "text", "weight", "is_required", "section"} <= set(rows[0])
    assert any(r["is_required"] for r in rows)


def test_the_checks_endpoint_requires_a_login(client: Any) -> None:
    assert client.get(f"{URL}completeness-checks/").status_code in (401, 403)
