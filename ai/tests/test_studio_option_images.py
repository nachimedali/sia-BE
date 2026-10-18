"""Picture tiles for the Studio's look controls.

Scene, light, camera, cast and vibe options carry a thumbnail
(`metadata.image`, a path under the frontend's `public/`). It is data on the
row: a tile with no picture keeps its icon, an operator's own picture is never
overwritten, and the file a row points at has to exist.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command

from ai.creative_seed import CATALOG, STUDIO_IMAGES
from ai.models import CreativeOption

MIGRATION = importlib.import_module("ai.migrations.0019_studio_option_images")
PUBLIC = Path(__file__).resolve().parents[3] / "app-FE" / "public"
URL = "/api/v1/ai/creative-options/"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seeding_on(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def _path(kind: str, key: str) -> str:
    return f"/studio/{kind}/{key}.webp"


def test_every_look_option_but_one_has_a_picture() -> None:
    assert set(STUDIO_IMAGES) == {"scene", "light", "camera", "cast", "vibe"}
    for kind, keys in STUDIO_IMAGES.items():
        seeded = {row["key"] for row in CATALOG[kind]}
        assert set(keys) <= seeded, f"{kind}: a picture for a key that is not seeded"
    # Tight crop is waiting on its picture; the tile keeps its icon until then.
    assert {r["key"] for r in CATALOG["camera"]} - set(STUDIO_IMAGES["camera"]) == {"close-crop"}
    for kind in ("scene", "light", "cast", "vibe"):
        assert {r["key"] for r in CATALOG[kind]} == set(STUDIO_IMAGES[kind])


def test_the_seed_points_each_row_at_its_picture() -> None:
    for kind, keys in STUDIO_IMAGES.items():
        for row in CATALOG[kind]:
            if row["key"] in keys:
                assert row["metadata"]["image"] == _path(kind, row["key"])
            else:
                assert "image" not in row["metadata"]


def test_every_picture_a_row_points_at_exists_and_is_small() -> None:
    if not PUBLIC.is_dir():
        pytest.skip("frontend not checked out beside the backend")
    for kind, keys in STUDIO_IMAGES.items():
        for key in keys:
            file = PUBLIC / _path(kind, key).lstrip("/")
            assert file.is_file(), file
            assert file.stat().st_size < 40_000, f"{file.name} is too heavy for a tile"


def test_seeding_writes_the_picture_and_refresh_keeps_it() -> None:
    call_command("seed_creative_options", verbosity=0)
    row = CreativeOption.objects.get(kind="scene", key="desert-dunes")
    assert row.metadata["image"] == _path("scene", "desert-dunes")
    call_command("seed_creative_options", "--refresh", verbosity=0)
    row.refresh_from_db()
    assert row.metadata["image"] == _path("scene", "desert-dunes")


def test_the_catalog_endpoint_serves_the_picture(
    auth_client: Any, workspace: Any, generation_costs: None
) -> None:
    call_command("seed_creative_options", verbosity=0)
    options = auth_client.get(URL).json()["options"]
    by_key = {row["key"]: row for row in options["camera"]}
    assert by_key["macro"]["metadata"]["image"] == _path("camera", "macro")
    assert "image" not in by_key["close-crop"]["metadata"]


def test_the_migration_fills_existing_rows_and_leaves_operator_edits_alone() -> None:
    plain = CreativeOption.objects.create(
        kind="scene", key="desert-dunes", label="x", metadata={"seed": "v2"}
    )
    own = CreativeOption.objects.create(
        kind="scene", key="city-street", label="x", metadata={"image": "/mine.webp"}
    )
    none_yet = CreativeOption.objects.create(
        kind="camera", key="close-crop", label="x", metadata={"seed": "v2"}
    )
    other_kind = CreativeOption.objects.create(
        kind="mood", key="desert-dunes", label="x", metadata={}
    )

    MIGRATION.forwards(apps, None)

    for row in (plain, own, none_yet, other_kind):
        row.refresh_from_db()
    assert plain.metadata == {"seed": "v2", "image": _path("scene", "desert-dunes")}
    assert own.metadata["image"] == "/mine.webp"
    assert "image" not in none_yet.metadata
    assert "image" not in other_kind.metadata


def test_the_migration_is_off_when_seeding_is_off(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = CreativeOption.objects.create(kind="scene", key="desert-dunes", label="x", metadata={})
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert "image" not in row.metadata
