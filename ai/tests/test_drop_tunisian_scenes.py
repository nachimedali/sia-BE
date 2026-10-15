from __future__ import annotations

import importlib

import pytest
from django.apps import apps

from ai.creative_seed import CATALOG
from ai.models import CreativeOption

MIGRATION = importlib.import_module("ai.migrations.0018_drop_tunisian_scenes")
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seeding_on(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def test_the_seed_holds_no_tunisian_scene_or_preset() -> None:
    assert not {r["key"] for r in CATALOG["scene"]} & set(MIGRATION.SCENES)
    assert not {r["key"] for r in CATALOG["preset"]} & set(MIGRATION.PRESETS)
    for row in CATALOG["scene"] + CATALOG["preset"]:
        assert "tunis" not in str(row).lower()


def test_an_existing_database_loses_the_rows_and_keeps_an_operators_own() -> None:
    seeded = CreativeOption.objects.create(
        kind="scene", key="medina-souk", label="x", metadata={"seed": "v2"}
    )
    own = CreativeOption.objects.create(
        kind="scene", key="sidi-bou-said", label="mine", metadata={}
    )
    CreativeOption.objects.create(
        kind="preset",
        key="ramadan-evenings",
        label="r",
        metadata={"values": {"scene": "tunis-rooftop"}},
    )

    MIGRATION.forwards(apps, None)

    assert not CreativeOption.objects.filter(pk=seeded.pk).exists()
    assert CreativeOption.objects.filter(pk=own.pk).exists()
    preset = CreativeOption.objects.get(key="ramadan-evenings")
    assert preset.metadata["values"]["scene"] == "garden-terrace"
