"""S17-27 — a template's place is a catalogue row (`studio_content_audit.md` §3).

One `place` kind replaces the code dictionary: each row holds the set the camera
sees, a light it is locked to, where the product stands and what the place must
never show — plus its picture and its French and Italian names. A new place is
a row in admin. The person's own light wins over the place's; what the place
forbids joins the negative; a brand-colour set takes the kit's primary.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command

from ai.ad_template_seed import PLACES, TEMPLATES
from ai.models import CreativeKind, CreativeOption, Generation
from ai.services import ad_templates
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    KEYFRAME,
    catalog,
    flag_on,
    growth,
    make_product,
)
from brand.models import BrandCore

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ai.migrations.0038_template_places")
PLACE_KEYS = {place["key"] for place in PLACES}


def _row(key: str) -> CreativeOption:
    return CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key=key)


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_every_place_says_what_the_camera_sees_how_it_is_lit_and_what_it_forbids() -> None:
    assert len(PLACE_KEYS) == len(PLACES)
    for place in PLACES:
        meta = place["metadata"]
        for field in ("set", "light", "zone", "forbid"):
            assert isinstance(meta[field], str) and meta[field].strip(), (place["key"], field)
        assert meta["image"] == f"/studio/places/{place['key']}.webp", place["key"]
        assert place["prompt_fragment"] == meta["set"]
        assert len(meta["set"]) <= 300, place["key"]


def test_every_template_is_set_only_in_places_the_catalogue_has() -> None:
    for spec in TEMPLATES:
        missing = set(spec["metadata"]["locations"]) - PLACE_KEYS
        assert not missing, (spec["key"], missing)


def test_places_that_failed_the_build_test_are_gone() -> None:
    assert not {"airport", "dunes"} & PLACE_KEYS
    new = {"shop-counter", "salon", "bathroom-shelf", "bedside", "cafe-table"}
    assert new | {"car-interior", "entryway", "picnic", "brand-colour"} <= PLACE_KEYS


def test_the_seed_command_creates_translated_places(catalog: None) -> None:  # noqa: F811
    rows = CreativeOption.objects.filter(kind=CreativeKind.PLACE)
    assert {row.key for row in rows} == PLACE_KEYS
    for row in rows:
        assert row.translations["fr"]["label"] and row.translations["it"]["label"], row.key


# -----------------------------------------------------------------------------
# What reaches the image model
# -----------------------------------------------------------------------------
def test_the_place_sets_the_scene_light_and_product_and_its_forbids_join_the_negative(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    response = auth_client.post(
        KEYFRAME,
        {"template": "pour-shot", "product": product.pk, "location": "cafe-table"},
        format="json",
    )
    assert response.status_code in (200, 201, 202), response.json()

    place = CreativeOption.objects.get(kind=CreativeKind.PLACE, key="cafe-table").metadata
    generation = Generation.objects.filter(workspace=growth).latest("id")
    sent = generation.prompt_sent
    assert place["set"] in sent and place["light"] in sent and place["zone"] in sent
    assert generation.creative["negative"].endswith(place["forbid"])
    assert f"Avoid: {generation.creative['negative']}." in sent


def test_the_persons_own_light_wins_over_the_places(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    row = _row("pour-shot")
    place = CreativeOption.objects.get(kind=CreativeKind.PLACE, key="cafe-table").metadata

    lit = ad_templates.keyframe_prompt(row, product=product, location="cafe-table")
    chosen = ad_templates.keyframe_prompt(
        row, product=product, location="cafe-table", own_light=True
    )

    assert place["light"] in lit and place["light"] not in chosen
    assert place["set"] in chosen


def test_the_brand_colour_set_takes_the_kits_primary(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    row = _row("hero-orbit")
    plain = ad_templates.keyframe_prompt(row, product=product, location="brand-colour")
    BrandCore.objects.create(
        workspace=growth,
        version=1,
        sections={
            "palette": {
                "value": {"colors": [{"role": "Primary", "hex": "#1F4E5A"}], "fonts": {}},
                "origin": "site",
            }
        },
    )
    branded = ad_templates.keyframe_prompt(
        row, product=product, location="brand-colour", workspace=growth
    )

    assert "{brand_colour}" not in plain and "#1F4E5A" not in plain
    assert "#1F4E5A" in branded


def test_an_unknown_place_is_still_a_400(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    response = auth_client.post(
        KEYFRAME,
        {"template": "pour-shot", "product": product.pk, "location": "moon"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "template_location"


def test_a_place_an_operator_retired_falls_back_to_its_key(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """A template row still naming a place with no row (an operator's list, or
    one this migration left alone) renders with the key's words, as before."""
    product = make_product(growth, make_png_upload)
    row = _row("pour-shot")
    row.metadata = {**row.metadata, "locations": ["harbour"]}
    row.save(update_fields=["metadata"])

    assert "Setting: harbour" in ad_templates.keyframe_prompt(row, product=product)


# -----------------------------------------------------------------------------
# The gallery
# -----------------------------------------------------------------------------
def test_the_gallery_names_each_place_in_the_interface_language_with_its_picture(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = auth_client.get(f"{GALLERY}?product={product.pk}", HTTP_ACCEPT_LANGUAGE="fr").json()

    places = {place["key"]: place for place in body["locations"]}
    cafe = CreativeOption.objects.get(kind=CreativeKind.PLACE, key="cafe-table")
    assert places["cafe-table"]["label"] == cafe.translations["fr"]["label"]
    assert places["cafe-table"]["image"] == cafe.metadata["image"]
    used = {key for template in body["templates"] for key in template["locations"]}
    assert set(places) == used


# -----------------------------------------------------------------------------
# The migration
# -----------------------------------------------------------------------------
@pytest.fixture
def seeding(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def _old(key: str) -> CreativeOption:
    spec = copy.deepcopy(next(t for t in TEMPLATES if t["key"] == key))
    spec["metadata"]["locations"] = list(MIGRATION.OLD_LOCATIONS[key])
    return CreativeOption.objects.create(
        kind="ad_template", key=key, label=spec["label"], metadata=spec["metadata"]
    )


def test_the_migration_seeds_places_and_moves_untouched_place_lists(seeding: None) -> None:
    levitate = _old("levitate")
    gate = _old("gate-app")
    gate.metadata = {**gate.metadata, "locations": ["airport", "harbour"]}  # an operator's
    gate.save(update_fields=["metadata"])

    MIGRATION.forwards(apps, None)
    MIGRATION.forwards(apps, None)  # twice: nothing more

    assert (
        set(CreativeOption.objects.filter(kind="place").values_list("key", flat=True)) == PLACE_KEYS
    )
    levitate.refresh_from_db()
    gate.refresh_from_db()
    new = next(t for t in TEMPLATES if t["key"] == "levitate")["metadata"]["locations"]
    assert levitate.metadata["locations"] == new
    assert gate.metadata["locations"] == ["airport", "harbour"]


def test_the_migration_is_off_under_test_settings(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    MIGRATION.forwards(apps, None)
    assert not CreativeOption.objects.filter(kind="place").exists()


def test_every_recorded_old_list_differs_from_the_new_one() -> None:
    seeded = {t["key"]: t for t in TEMPLATES}
    for key, old in MIGRATION.OLD_LOCATIONS.items():
        if key not in seeded:  # retired since (S17-32)
            continue
        new = seeded[key]["metadata"]["locations"]
        assert list(old) != new, key


def test_seeding_twice_keeps_an_operators_place_edit(catalog: None) -> None:  # noqa: F811
    CreativeOption.objects.filter(kind="place", key="kitchen").update(label="Our kitchen")
    call_command("seed_creative_options", verbosity=0)
    assert CreativeOption.objects.get(kind="place", key="kitchen").label == "Our kitchen"
