"""`ai/migrations/0011_catalog_v2` replaces the first-generation seed, moves
products onto the new keys, and leaves everything else alone."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from django.apps import apps

from ai.creative_seed import CATALOG
from ai.models import CreativeOption

pytestmark = pytest.mark.django_db

MIGRATION = import_module("ai.migrations.0011_catalog_v2")


@pytest.fixture(autouse=True)
def _seeding_on(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True  # off under test settings


def _v1_row(kind: str, key: str) -> CreativeOption:
    """A first-generation row: no `metadata.seed`."""
    return CreativeOption.objects.create(kind=kind, key=key, label=key, metadata={})


@pytest.fixture
def v1_catalog() -> None:
    for kind, keys in MIGRATION.V1_KEYS.items():
        for key in keys:
            _v1_row(kind, key)


def run() -> None:
    MIGRATION.forwards(apps, None)


def test_the_first_generation_is_replaced_by_v2(v1_catalog: None) -> None:
    run()

    assert not CreativeOption.objects.filter(kind="scene", key="sidibou").exists()
    assert not CreativeOption.objects.filter(kind="light", key="golden").exists()
    expected = sum(len(rows) for rows in CATALOG.values())
    assert CreativeOption.objects.count() == expected
    # Same-named rows are v2 now, not the old content.
    hands = CreativeOption.objects.get(kind="cast", key="hands")
    assert hands.metadata["seed"] == "v2" and "natural skin texture" in hands.prompt_fragment


def test_an_operators_own_rows_and_edits_survive(v1_catalog: None) -> None:
    custom = CreativeOption.objects.create(
        kind="scene", key="harbour", label="Harbour", metadata={}
    )
    run()
    edited = CreativeOption.objects.get(kind="scene", key="seamless-studio")
    edited.label = "Edited in admin"
    edited.save()

    run()

    assert CreativeOption.objects.filter(pk=custom.pk).exists()
    assert CreativeOption.objects.get(pk=edited.pk).label == "Edited in admin"


def test_running_it_again_changes_nothing(v1_catalog: None) -> None:
    run()
    before = list(
        CreativeOption.objects.order_by("kind", "key").values_list("kind", "key", "label")
    )

    run()

    assert (
        list(CreativeOption.objects.order_by("kind", "key").values_list("kind", "key", "label"))
        == before
    )


def test_products_move_onto_the_new_keys(v1_catalog: None, workspace: Any) -> None:
    from products.models import Product

    product = Product.objects.create(
        workspace=workspace,
        name="Olive oil",
        scenes=["djerba", "studio", "gone"],
        lights=["golden", "noon"],
        people="one",
        ctas=["shop"],
        aspects=["4-5"],
        audience=["home-cooks", "retired-key"],
        languages=["fr"],
        tone_preset="artisan",
        claims=[{"key": "organic", "proof_media": None}, {"key": "unknown", "proof_media": None}],
        reference_tags={"12": "front", "13": "nonsense"},
    )

    run()

    product.refresh_from_db()
    assert product.scenes == ["beach-shoreline", "seamless-studio"]
    assert product.lights == ["golden-hour", "hard-sun"]
    assert product.people == "model"
    assert product.ctas == ["shop-now"]
    assert product.aspects == ["4-5"]
    assert product.audience == ["home-cooks"]
    assert product.languages == ["fr"]
    assert product.tone_preset == "artisan"
    assert product.claims == [{"key": "organic", "proof_media": None}]
    assert product.reference_tags == {"12": "front"}


def test_it_does_nothing_with_seeding_off(settings: Any, v1_catalog: None) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False

    run()

    assert CreativeOption.objects.filter(kind="scene", key="sidibou").exists()


def test_past_generations_keep_their_choices_under_the_new_keys(
    v1_catalog: None, workspace: Any, user: Any
) -> None:
    from ai.models import Generation

    generation = Generation.objects.create(
        workspace=workspace,
        user=user,
        kind="IMAGE",
        mode="IDEA",
        creative={
            "scene": "djerba",
            "light": "golden",
            "camera": "eye",
            "format": "story",
            "tone": "formal",
            "moods": ["minimal", "warm"],
            "toggles": {"hashtags": True},
        },
    )

    run()

    generation.refresh_from_db()
    assert generation.creative == {
        "scene": "beach-shoreline",
        "light": "golden-hour",
        "camera": "eye-level",
        "format": "vertical",
        "tone": "refined",
        "moods": ["clean", "warm"],
        "toggles": {"hashtags": True},
    }


def test_every_first_generation_studio_choice_has_a_v2_equivalent() -> None:
    v2 = {kind: {row["key"] for row in rows} for kind, rows in CATALOG.items()}
    for kind in (*MIGRATION.GENERATION_SINGLES, "mood"):
        for key in MIGRATION.V1_KEYS[kind]:
            assert MIGRATION.RENAMED.get(kind, {}).get(key, key) in v2[kind], (kind, key)
