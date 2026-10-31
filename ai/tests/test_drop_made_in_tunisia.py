"""`0032_drop_made_in_tunisia`: the Tunisia-specific claim and word chip leave the catalog.

Same shape as `0018_drop_tunisian_scenes`: only rows still carrying the seed
marker go, an operator's own row with the same key stays, and products drop
the claim key so their next save passes the catalog check.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps

from ai.creative_seed import CATALOG, CLAIM_PHRASES
from ai.models import CreativeOption

MIGRATION = importlib.import_module("ai.migrations.0032_drop_made_in_tunisia")
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seeding_on(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def test_the_seed_holds_nothing_about_tunisia() -> None:
    assert "made-in-tunisia" not in CLAIM_PHRASES
    for kind, rows in CATALOG.items():
        for row in rows:
            assert "tunis" not in str(row).lower(), (kind, row["key"])


def test_an_existing_database_loses_the_seeded_rows_and_products_drop_the_claim(
    workspace, make_png_upload
) -> None:
    from products.models import Product

    claim = CreativeOption.objects.create(
        kind="claim", key="made-in-tunisia", label="Made in Tunisia", metadata={"seed": "v2"}
    )
    chip = CreativeOption.objects.create(
        kind="suggestion",
        key="use_words-made-in-tunisia",
        label="made in Tunisia",
        metadata={"seed": "v2", "field": "use_words"},
    )
    product = Product.objects.create(
        workspace=workspace,
        name="Oil",
        claims=[{"key": "made-in-tunisia", "proof_media": None}, {"key": "organic"}],
        use_words=["made in Tunisia", "craft"],
    )

    MIGRATION.forwards(apps, None)

    assert not CreativeOption.objects.filter(pk__in=[claim.pk, chip.pk]).exists()
    product.refresh_from_db()
    assert product.claims == [{"key": "organic"}]
    assert product.use_words == ["made in Tunisia", "craft"]  # the brand's own words stay


def test_an_operators_own_row_with_the_key_stays() -> None:
    own = CreativeOption.objects.create(
        kind="claim", key="made-in-tunisia", label="Made here", metadata={}
    )
    MIGRATION.forwards(apps, None)
    assert CreativeOption.objects.filter(pk=own.pk).exists()


def test_off_under_test_settings(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = CreativeOption.objects.create(
        kind="claim", key="made-in-tunisia", label="x", metadata={"seed": "v2"}
    )
    MIGRATION.forwards(apps, None)
    assert CreativeOption.objects.filter(pk=row.pk).exists()
