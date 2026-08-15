"""`seed_trend_sources` (implementation.md §7)."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from categories.models import Category
from content.models import Platform
from trends.models import TrendSource, TrendSourceKind

pytestmark = pytest.mark.django_db


def _seed() -> str:
    out = StringIO()
    call_command("seed_trend_sources", stdout=out)
    return out.getvalue()


def test_seeds_sources_for_every_root_and_platform(category: Any) -> None:
    _seed()

    root = category.parent
    assert TrendSource.objects.filter(category=root).exists()
    # Every platform the seed covers gets at least one source, and none hang
    # off the leaf — inheritance is what serves leaves (`sources_for`).
    assert set(TrendSource.objects.filter(category=root).values_list("platform", flat=True)) == set(
        Platform.values
    )
    assert not TrendSource.objects.filter(category=category).exists()


def test_the_seed_is_idempotent(category: Any) -> None:
    _seed()
    first = TrendSource.objects.count()

    output = _seed()

    assert TrendSource.objects.count() == first
    assert "0 created" in output


def test_re_seeding_does_not_reset_a_watermark(category: Any) -> None:
    """A deploy re-asserting the defaults must not make every source re-ingest
    its whole history."""
    from django.utils import timezone

    _seed()
    source = TrendSource.objects.first()
    assert source is not None
    source.watermark_at = timezone.now()
    source.save(update_fields=["watermark_at"])

    _seed()

    source.refresh_from_db()
    assert source.watermark_at is not None


def test_each_kind_carries_the_vendor_that_answers_for_it(category: Any) -> None:
    _seed()

    vendors = dict(TrendSource.objects.values_list("kind", "vendor"))
    assert vendors[TrendSourceKind.ADLIB] == "rapidapi"
    assert vendors[TrendSourceKind.YOUTUBE] == "youtube"


def test_seeding_without_categories_says_so(db: None) -> None:
    Category.objects.all().delete()
    err = StringIO()
    call_command("seed_trend_sources", stderr=err)

    assert "seed_categories" in err.getvalue()
    assert not TrendSource.objects.exists()
