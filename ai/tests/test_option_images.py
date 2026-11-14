"""Every Studio choice is pictured by its own still.

A place, an avatar trait, an angle and a template with no preview clip each
name a static file the frontend serves (`/studio/<dir>/<key>.webp`), the way
the look options already do (`0019_studio_option_images`). The files are drawn
from `studio_previews.md`; until one exists the card shows its placeholder.

Places used to borrow a scene's still, so eight of them shared four pictures;
`0048_option_images` gives each its own, and leaves a picture an operator set
in admin alone.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import pytest
from django.apps import apps

from ai.ad_template_seed import ANGLES, AVATAR_TRAITS, PLACES, PREVIEWS, TEMPLATES
from ai.models import CreativeOption
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    catalog,
    flag_on,
    growth,
    make_product,
)

MIGRATION = importlib.import_module("ai.migrations.0048_option_images")
pytestmark = pytest.mark.django_db
LIBRARY = "/api/v1/brand/avatars/"


@pytest.mark.parametrize(
    ("rows", "folder"),
    [(PLACES, "places"), (AVATAR_TRAITS, "avatar"), (ANGLES, "angles")],
)
def test_each_choice_names_its_own_still(rows: list[dict[str, Any]], folder: str) -> None:
    for spec in rows:
        assert spec["metadata"]["image"] == f"/studio/{folder}/{spec['key']}.webp"


def test_a_template_with_no_clip_names_its_still() -> None:
    for spec in TEMPLATES:
        meta = spec["metadata"]
        assert meta["poster"] == f"/studio/templates/{spec['key']}.webp"
        assert ("preview" in meta) == (spec["key"] in PREVIEWS)


def test_the_gallery_serves_each_angles_still(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = auth_client.get(f"{GALLERY}?product={product.pk}").json()
    angles = {row["key"]: row for row in body["angles"]}
    assert angles["objection"]["image"] == "/studio/angles/objection.webp"
    places = {row["key"]: row for row in body["locations"]}
    assert places["picnic"]["image"] == "/studio/places/picnic.webp"
    templates = {row["key"]: row for row in body["templates"]}
    assert templates["levitate"]["poster"] == "/studio/templates/levitate.webp"
    assert templates["levitate"]["preview"] == ""


def test_the_avatar_library_serves_each_traits_still(
    auth_client: Any,
    growth: Any,  # noqa: F811
) -> None:
    traits = {row["key"]: row for row in auth_client.get(LIBRARY).json()["traits"]}
    assert traits["hair-curly"]["image"] == "/studio/avatar/hair-curly.webp"
    assert traits["arch-chef"]["image"] == "/studio/avatar/arch-chef.webp"


# -----------------------------------------------------------------------------
# `0048_option_images`: rows seeded before the stills
# -----------------------------------------------------------------------------
@pytest.fixture
def seeding_on(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def _old_row(kind: str, rows: list[dict[str, Any]], key: str, **meta: Any) -> CreativeOption:
    old = copy.deepcopy(next(spec for spec in rows if spec["key"] == key)["metadata"])
    for field in ("image", "poster"):
        old.pop(field, None)
    return CreativeOption.objects.create(kind=kind, key=key, label=key, metadata={**old, **meta})


def test_a_place_on_a_borrowed_scene_takes_its_own(seeding_on: None) -> None:
    row = _old_row("place", PLACES, "picnic", image="/studio/scene/botanical-set.webp")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata["image"] == "/studio/places/picnic.webp"


def test_traits_angles_and_clipless_templates_take_theirs(seeding_on: None) -> None:
    trait = _old_row("avatar_trait", AVATAR_TRAITS, "hair-curly")
    angle = _old_row("angle", ANGLES, "proof")
    template = _old_row("ad_template", TEMPLATES, "levitate")
    MIGRATION.forwards(apps, None)
    for row in (trait, angle, template):
        row.refresh_from_db()
    assert trait.metadata["image"] == "/studio/avatar/hair-curly.webp"
    assert angle.metadata["image"] == "/studio/angles/proof.webp"
    assert template.metadata["poster"] == "/studio/templates/levitate.webp"


def test_an_operators_own_picture_stays(seeding_on: None) -> None:
    place = _old_row("place", PLACES, "picnic", image="/media/ours.webp")
    trait = _old_row("avatar_trait", AVATAR_TRAITS, "hair-curly", image="/media/hair.webp")
    MIGRATION.forwards(apps, None)
    place.refresh_from_db()
    trait.refresh_from_db()
    assert place.metadata["image"] == "/media/ours.webp"
    assert trait.metadata["image"] == "/media/hair.webp"


def test_a_template_with_a_clip_keeps_its_first_frame(seeding_on: None) -> None:
    row = _old_row("ad_template", TEMPLATES, "pour-shot", poster="/media/frame.webp")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata["poster"] == "/media/frame.webp"


def test_nothing_changes_with_seeding_off(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = _old_row("place", PLACES, "picnic", image="/studio/scene/botanical-set.webp")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata["image"] == "/studio/scene/botanical-set.webp"
