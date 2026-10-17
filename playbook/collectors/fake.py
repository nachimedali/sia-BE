"""The deterministic stand-in for both providers (rule 6, rule 17).

A fresh checkout fills the Playbook with no provider account at all. Every post
it makes is marked `is_fake`, and the API serves none of them unless
`PLAYBOOK_SHOW_SAMPLE` is on — which the production settings refuse — so a
fixture never reaches a customer as a trend.

It is deterministic (the same source yields the same posts, so a test asserts
on content) and honest about absence: it reads `reports` off the real adapter
and leaves every count the real provider does not publish unset.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

from playbook.collectors.base import CollectedPost, CollectRequest
from playbook.collectors.http import normalise_hashtags
from playbook.models import PostFormat

if TYPE_CHECKING:
    from playbook.models import PlaybookSource

#: Hooks per root category slug, so a sample about coffee never says "kilim".
HOOKS: dict[str, list[str]] = {
    "food-beverage": [
        "Pouring the first press at sunrise",
        "Three ways with one bottle of olive oil",
        "The iftar table, top-down",
        "Grandma's harissa, step by step",
        "Mint tea poured from a metre high",
        "The 7:10 espresso ritual",
    ],
    "home-lifestyle": [
        "Painting a Nabeul plate in one take",
        "Glaze reveal from the kiln",
        "A kilim from first thread to last",
        "Candles that smell like Sidi Bou Said",
        "Rooftop dinner, styled in 30 seconds",
        "Brass lanterns from the souk",
    ],
    "fashion-apparel": [
        "Five ways to wear a fouta",
        "Silver from the Djerba souk",
        "Sandals stitched by hand",
        "Beach to dinner, one outfit",
    ],
    "beauty-wellness": [
        "Jasmine soap, cut and stamped",
        "Hammam ritual at home",
        "Argan oil, from nut to bottle",
        "Morning routine with three drops",
    ],
}
GENERIC_HOOKS = [
    "Behind the scenes, one take",
    "Before and after in ten seconds",
    "What one day of making looks like",
    "The detail nobody notices",
]
SEARCHES = ["Ramadan recipes", "Olive harvest", "Summer in Djerba", "Back to school", "Eid gifts"]
CREATORS = [
    "dar.zitouna",
    "kahwa.club",
    "lalla.kitchen",
    "atelier.sidi",
    "djerba.days",
    "souk.stories",
]
DEFAULT_ITEMS = 6
#: How many sample pictures the frontend ships (`public/playbook-sample/`).
SAMPLE_IMAGES = 6


def _seeded(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def _sample_image(n: int) -> str:
    base = getattr(settings, "PLAYBOOK_SAMPLE_IMAGE_BASE", "")
    return f"{base}{n % SAMPLE_IMAGES + 1}.svg" if base else ""


@dataclass
class FakeCollector:
    provider: str
    items_per_request: int = DEFAULT_ITEMS
    fail_with: Exception | None = None
    calls: list[CollectRequest] = field(default_factory=list)

    @property
    def reports(self) -> frozenset[str]:
        from playbook.collectors.registry import REAL

        return REAL[self.provider]().reports

    def plan(self, source: PlaybookSource) -> list[CollectRequest]:
        from playbook.collectors.registry import REAL

        planned = REAL[self.provider]().plan(source)
        slug = source.category.slug if source.category is not None else ""
        return [
            CollectRequest(r.provider, params={**r.params, "_slug": slug}, config=r.config)
            for r in planned
        ]

    def fetch(self, request: CollectRequest) -> Iterable[CollectedPost]:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        if self.provider == "google_trends":
            return [self._search(request, i) for i in range(min(len(SEARCHES), 5))]
        return [self._video(request, i) for i in range(self.items_per_request)]

    def _video(self, request: CollectRequest, i: int) -> CollectedPost:
        slug = str(request.params.get("_slug") or "")
        key = str(request.params.get("q") or request.params.get("chart") or "chart")
        hooks = HOOKS.get(slug, GENERIC_HOOKS)
        seed = _seeded(self.provider, slug, key, str(i))
        seconds = 12 + seed % 150
        views = 2_000 + seed % 400_000
        hours = 1 + seed % (24 * 20)
        external = f"fake-{seed:08x}"
        title = hooks[(seed // 7) % len(hooks)]
        return CollectedPost(
            external_id=external,
            posted_at=timezone.now() - dt.timedelta(hours=hours),
            format=PostFormat.SHORT if seconds <= 60 else PostFormat.VIDEO,
            url=f"https://www.youtube.com/watch?v={external}",
            title=title,
            caption=f"{title}. Sample post from the fake collector.",
            thumbnail_url=_sample_image(seed),
            creator=CREATORS[seed % len(CREATORS)],
            hashtags=normalise_hashtags(title.split()[:2]),
            duration_seconds=seconds,
            views=views,
            # Every third sample hides its like count, as real channels do,
            # so the null path is exercised rather than assumed.
            likes=None if i % 3 == 2 else views * (2 + seed % 9) // 100,
            comments=views * (1 + seed % 3) // 1000,
            payload={"fake": True},
            is_fake=True,
        )

    def _search(self, request: CollectRequest, i: int) -> CollectedPost:
        geo = str(request.params.get("geo") or "")
        seed = _seeded(self.provider, geo, str(i))
        floor = (1 + seed % 20) * 1000
        title = SEARCHES[i]
        return CollectedPost(
            external_id=f"{geo}:{title.lower()}",
            posted_at=timezone.now() - dt.timedelta(hours=1 + seed % 20),
            format=PostFormat.SEARCH,
            url=f"https://www.google.com/search?q={title.replace(' ', '+')}",
            title=title,
            caption="Sample search from the fake collector.",
            hashtags=normalise_hashtags([title]),
            traffic_floor=floor,
            traffic_label=f"{floor:,}+",
            news=[{"title": f"{title}: what to know", "url": "", "source": "Sample news"}],
            payload={"fake": True},
            is_fake=True,
        )
