"""Fixtures for the Playbook suite."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from django.utils import timezone

from ai.providers import reference_images
from categories.models import Category
from playbook.collectors import registry
from playbook.models import PlaybookPost, PlaybookSource, PostFormat


@pytest.fixture(autouse=True)
def _clean_ports() -> Iterator[None]:
    registry.clear_overrides()
    reference_images.set_override(None)
    yield
    registry.clear_overrides()
    reference_images.set_override(None)


@pytest.fixture
def food(db: None) -> Category:
    return Category.objects.create(name="Food & Beverage", slug="food-beverage")


@pytest.fixture
def home(db: None) -> Category:
    return Category.objects.create(name="Home & Lifestyle", slug="home-lifestyle")


@pytest.fixture
def market_source(db: None) -> PlaybookSource:
    return PlaybookSource.objects.create(
        provider="youtube", region="TN", config={"chart": True}, refresh_minutes=120
    )


@pytest.fixture
def trends_source(db: None) -> PlaybookSource:
    return PlaybookSource.objects.create(provider="google_trends", region="TN", refresh_minutes=60)


@pytest.fixture
def make_post(market_source: PlaybookSource) -> Any:
    counter = {"n": 0}

    def _make(**fields: Any) -> PlaybookPost:
        counter["n"] += 1
        now = timezone.now()
        defaults: dict[str, Any] = {
            "source": market_source,
            "provider": market_source.provider,
            "region": market_source.region,
            "external_id": f"vid-{counter['n']}",
            "format": PostFormat.SHORT,
            "url": f"https://www.youtube.com/watch?v=vid-{counter['n']}",
            "thumbnail_url": f"https://i.ytimg.com/vi/vid-{counter['n']}/hqdefault.jpg",
            "title": f"Post {counter['n']}",
            "creator": "dar.zitouna",
            "posted_at": now - dt.timedelta(hours=10),
            "views": 1000,
            "likes": 50,
            "comments": 5,
            "last_seen_at": now,
        }
        defaults.update(fields)
        return PlaybookPost.objects.create(**defaults)

    return _make
