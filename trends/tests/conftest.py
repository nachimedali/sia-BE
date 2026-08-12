from __future__ import annotations

from typing import Any

import pytest

from content.models import Platform
from trends.models import TrendSourceKind


@pytest.fixture(autouse=True)
def _isolate_trend_vendors() -> Any:
    """The fake vendors are module-level singletons (mirroring the AI and
    platform fakes), so their recorded calls have to be cleared between tests
    or one test's assertions read another's."""
    from trends.providers.fake import clear_fake_vendors

    clear_fake_vendors()
    yield
    clear_fake_vendors()


@pytest.fixture
def category(db: None) -> Any:
    from categories.models import Category

    root = Category.objects.create(name="Home & Lifestyle", slug="home-lifestyle")
    return Category.objects.create(name="Homeware & Ceramics", slug="ceramics", parent=root)


@pytest.fixture
def source(category: Any) -> Any:
    """One Creative Center source on the *root*, so every test also exercises
    the inheritance `seed_trend_sources` relies on."""
    from trends.models import TrendSource

    return TrendSource.objects.create(
        category=category.parent,
        platform=Platform.TIKTOK,
        kind=TrendSourceKind.CREATIVE_CENTER,
        vendor="rapidapi",
        query={"q": "ceramics"},
    )


@pytest.fixture
def trend_workspace(paid_workspace: Any, category: Any) -> Any:
    """`paid_workspace` in the fixture category — trends are a paid feature
    (§4.1), and the teaser tests move it back to Free themselves."""
    paid_workspace.category = category
    paid_workspace.platforms = [Platform.TIKTOK]
    paid_workspace.save(update_fields=["category", "platforms"])
    return paid_workspace
