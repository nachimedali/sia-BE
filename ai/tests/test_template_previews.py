"""S17-39 — template preview clips on the gallery tiles (S17-17's interim).

A template row may name a short muted clip and its first frame, both static
files the frontend serves (`/studio/templates/<key>.mp4|.webp`), the way a
place row names its picture. The tile shows the still and plays the clip only
when asked, so a gallery of dozens loads stills alone. A template with no clip
yet serves blanks and keeps its drawn sketch.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError

from ai.ad_template_seed import PREVIEWS, TEMPLATES
from ai.models import CreativeOption
from ai.services.ad_templates import validate_metadata
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    catalog,
    flag_on,
    growth,
    make_product,
)

MIGRATION = importlib.import_module("ai.migrations.0046_template_previews")
pytestmark = pytest.mark.django_db


def _spec(key: str) -> dict[str, Any]:
    return next(spec for spec in TEMPLATES if spec["key"] == key)


def test_every_clip_belongs_to_a_template_in_the_seed() -> None:
    keys = {spec["key"] for spec in TEMPLATES}
    assert PREVIEWS and PREVIEWS.issubset(keys)


def test_the_seed_names_the_clip_and_its_first_frame() -> None:
    meta = _spec("pour-shot")["metadata"]
    assert meta["preview"] == "/studio/templates/pour-shot.mp4"
    assert meta["poster"] == "/studio/templates/pour-shot.webp"
    assert "preview" not in _spec("levitate")["metadata"]


def test_a_preview_must_be_text() -> None:
    meta = copy.deepcopy(_spec("pour-shot")["metadata"])
    validate_metadata(meta)
    meta["preview"] = 3
    with pytest.raises(ValidationError, match="preview"):
        validate_metadata(meta)


def test_the_gallery_serves_the_clip_or_blanks(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    rows = {
        row["key"]: row
        for row in auth_client.get(f"{GALLERY}?product={product.pk}").json()["templates"]
    }
    assert rows["pour-shot"]["preview"] == "/studio/templates/pour-shot.mp4"
    assert rows["pour-shot"]["poster"] == "/studio/templates/pour-shot.webp"
    assert rows["levitate"]["preview"] == "" and rows["levitate"]["poster"] == ""


# -----------------------------------------------------------------------------
# `0046_template_previews`: rows seeded before the clips take them
# -----------------------------------------------------------------------------
@pytest.fixture
def seeding_on(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def _old_row(key: str) -> CreativeOption:
    meta = copy.deepcopy(_spec(key)["metadata"])
    meta.pop("preview", None)
    meta.pop("poster", None)
    return CreativeOption.objects.create(kind="ad_template", key=key, label=key, metadata=meta)


def test_a_seeded_row_takes_its_clip(seeding_on: None) -> None:
    row = _old_row("get-ready")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata["preview"] == "/studio/templates/get-ready.mp4"
    assert row.metadata["poster"] == "/studio/templates/get-ready.webp"


def test_an_operators_own_clip_stays(seeding_on: None) -> None:
    row = _old_row("get-ready")
    row.metadata = {**row.metadata, "preview": "/ours.mp4", "poster": "/ours.webp"}
    row.save(update_fields=["metadata"])
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert (row.metadata["preview"], row.metadata["poster"]) == ("/ours.mp4", "/ours.webp")


def test_a_template_with_no_clip_is_left_alone(seeding_on: None) -> None:
    row = _old_row("levitate")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert "preview" not in row.metadata


def test_nothing_changes_with_seeding_off(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = _old_row("get-ready")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert "preview" not in row.metadata
