"""The seed lives in the migration chain, so `migrate` alone yields a working app.

Each seed migration's `forwards` is run directly against the live models (a
transactional test elsewhere may have truncated the rows `migrate` created, so
the test cannot assume they are still there). What it asserts is the contract:
the catalogue lands, a second run adds nothing, and an operator's edit survives.
"""

from importlib import import_module

import pytest
from django.apps import apps
from django.db.models import Model

from ai.models import CreativeOption, GenerationCost
from billing.models import Currency, Pack, Plan, PlanPrice
from categories.models import Category
from tools.models import ToolConfig
from trends.models import TrendSource

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seeding_on(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True  # off under test settings


# Order matters: trend sources hang off the categories.
SEEDS = [
    "billing.migrations.0018_seed_catalog",
    "ai.migrations.0008_seed_catalog",
    "ai.migrations.0009_revise_reason",
    "ai.migrations.0011_catalog_v2",
    "ai.migrations.0013_video_catalog",
    "ai.migrations.0017_italian_language",
    "categories.migrations.0002_seed_categories",
    "tools.migrations.0002_seed_tools",
    "trends.migrations.0004_seed_trend_sources",
]
MODELS: list[type[Model]] = [
    Currency,
    Plan,
    PlanPrice,
    Pack,
    CreativeOption,
    GenerationCost,
    Category,
    ToolConfig,
    TrendSource,
]


def _run_all() -> None:
    for path in SEEDS:
        import_module(path).forwards(apps, None)


def _counts() -> dict[str, int]:
    return {m.__name__: m._default_manager.count() for m in MODELS}


def test_the_seed_migrations_populate_every_catalogue_a_fresh_install_needs() -> None:
    _run_all()
    counts = _counts()
    assert all(n > 0 for n in counts.values()), counts
    assert Plan.objects.filter(code="trial").exists()  # registration provisions it
    assert CreativeOption.objects.filter(kind="scene").exists()  # the Studio's first control
    # S3: the Motion step's controls, each with its price on the row.
    for kind in ("video_length", "motion", "video_aspect", "reel_style", "music", "video_extra"):
        assert CreativeOption.objects.filter(kind=kind).exists(), kind
    # L-6 amended: Italian reaches a database seeded before it existed.
    assert CreativeOption.objects.filter(kind="language", key="it", is_active=True).exists()
    clip = CreativeOption.objects.get(kind="video_length", key="clip-5")
    assert clip.metadata["credits"] == 6
    assert TrendSource.objects.filter(workspace__isnull=True).count() == counts["TrendSource"]


def test_running_them_again_adds_nothing() -> None:
    _run_all()
    before = _counts()
    _run_all()
    assert _counts() == before


def test_an_operators_edit_is_never_overwritten() -> None:
    _run_all()
    Plan.objects.filter(code="trial").update(display_name="Edited in admin", monthly_ai_credits=7)
    scene = CreativeOption.objects.filter(kind="scene").first()
    cost = GenerationCost.objects.first()
    clip = CreativeOption.objects.get(kind="video_length", key="clip-5")
    assert scene and cost
    CreativeOption.objects.filter(pk=scene.pk).update(label="Retuned label", is_active=False)
    CreativeOption.objects.filter(pk=clip.pk).update(metadata={**clip.metadata, "credits": 9})
    GenerationCost.objects.filter(pk=cost.pk).update(credits=99)

    _run_all()

    assert Plan.objects.get(code="trial").display_name == "Edited in admin"
    assert Plan.objects.get(code="trial").monthly_ai_credits == 7
    scene.refresh_from_db()
    assert (scene.label, scene.is_active) == ("Retuned label", False)
    cost.refresh_from_db()
    assert cost.credits == 99
    clip.refresh_from_db()
    assert clip.metadata["credits"] == 9  # a retuned video price survives a re-run


def test_the_guard_keeps_the_suite_off_the_seed(settings) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    before = _counts()
    _run_all()
    assert _counts() == before


def test_socialapi_capability_rows_seed_unavailable_pairs_too() -> None:
    """SA-51: the rows that say *no* are what turn a vendor's `0` into a null,
    so they are seeded as deliberately as the ones that say yes — and an
    operator who flips one in admin keeps the flip."""
    from analytics.models import MetricCapability

    seed = import_module("analytics.migrations.0011_seed_socialapi_capabilities")
    seed.forwards(apps, None)
    rows = MetricCapability.objects.filter(provider_key="socialapi")

    assert rows.count() == len(seed.ROWS)
    facebook_saves = rows.get(platform="facebook", metric_key="saves")
    assert facebook_saves.available is False and facebook_saves.note
    instagram_reach = rows.get(platform="instagram", metric_key="reach")
    assert instagram_reach.available is True
    assert instagram_reach.latency_hint is not None
    assert instagram_reach.latency_hint.total_seconds() == 48 * 3600
    assert not rows.filter(platform="tiktok", available=True).exists()

    MetricCapability.objects.filter(pk=facebook_saves.pk).update(available=True, note="")
    seed.forwards(apps, None)

    assert rows.count() == len(seed.ROWS)
    assert rows.get(pk=facebook_saves.pk).available is True


def test_the_frozen_rows_match_the_capability_table_they_were_taken_from() -> None:
    """Drift between the two is allowed only through a new migration; until
    one exists, they say the same thing."""
    from social_providers.socialapi.capabilities import METRICS

    seed = import_module("analytics.migrations.0011_seed_socialapi_capabilities")
    live = {
        (platform, metric, row["available"], row["latency_hours"], row["note"])
        for platform, metrics in METRICS.items()
        for metric, row in metrics.items()
    }
    assert set(seed.ROWS) == live
