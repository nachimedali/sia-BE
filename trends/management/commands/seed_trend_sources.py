"""Seeds default trend sources per category and platform (implementation.md §7).

**Every root category, every platform, four source kinds** — but `is_active`
follows the platform, so seeding is cheap and switching a source on is a row
edit rather than a deploy (D12). Nothing is fetched here: sources describe where
to look, and looking happens on demand (D11).

Sources hang off **root** categories only. A child ("Homeware & Ceramics")
inherits its root's sources through `Category.ancestors()`, so adding a vertical
does not mean hand-writing six more vendor queries — and a category specific
enough to deserve its own source can be given one in admin.

Idempotent: `update_or_create` on the same uniqueness tuple the model enforces,
so re-running on deploy re-asserts the defaults without duplicating or
resetting a watermark.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from categories.models import Category
from content.models import Platform
from trends.models import TrendSource, TrendSourceKind

#: Which kinds are worth asking for each publishing platform. Ad Library covers
#: Meta's surfaces, Creative Center covers TikTok, YouTube is its own, and
#: Reddit is where people say why something worked rather than that it did.
PLATFORM_KINDS: dict[str, list[str]] = {
    Platform.INSTAGRAM: [TrendSourceKind.ADLIB, TrendSourceKind.REDDIT],
    Platform.FACEBOOK: [TrendSourceKind.ADLIB],
    Platform.TIKTOK: [TrendSourceKind.CREATIVE_CENTER, TrendSourceKind.REDDIT],
    Platform.YOUTUBE: [TrendSourceKind.YOUTUBE],
    Platform.THREADS: [TrendSourceKind.REDDIT],
    Platform.LINKEDIN: [TrendSourceKind.RSS],
}

#: Which vendor answers for each kind (D12). The value lands on the row, so
#: repointing a kind at a different vendor is an admin edit.
KIND_VENDORS: dict[str, str] = {
    TrendSourceKind.ADLIB: "rapidapi",
    TrendSourceKind.CREATIVE_CENTER: "rapidapi",
    TrendSourceKind.YOUTUBE: "youtube",
    TrendSourceKind.REDDIT: "reddit",
    TrendSourceKind.RSS: "rss",
}

#: Per-root query hints. Keyed by root slug; a root without an entry gets its
#: own name as the search term, which is the right default for every vendor
#: that takes one.
ROOT_QUERIES: dict[str, dict[str, Any]] = {
    "home-lifestyle": {"subreddit": "HomeDecorating"},
    "fashion-apparel": {"subreddit": "femalefashionadvice"},
    "food-beverage": {"subreddit": "FoodPorn"},
    "health-fitness": {"subreddit": "Fitness"},
    "beauty-wellness": {"subreddit": "SkincareAddiction"},
    "professional-services": {"subreddit": "smallbusiness"},
}


class Command(BaseCommand):
    help = "Seeds default trend sources for every root category and platform."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        roots = Category.objects.filter(parent__isnull=True, is_active=True)
        if not roots:
            self.stderr.write(
                self.style.WARNING("No root categories — run seed_categories first.")
            )
            return

        created = updated = 0
        for root in roots:
            hints = ROOT_QUERIES.get(root.slug, {})
            for platform, kinds in PLATFORM_KINDS.items():
                for kind in kinds:
                    _source, was_created = TrendSource.objects.update_or_create(
                        category=root,
                        platform=platform,
                        kind=kind,
                        vendor=KIND_VENDORS[kind],
                        defaults={
                            "query": {"q": root.name, **hints},
                            "is_active": True,
                        },
                    )
                    created += was_created
                    updated += not was_created

        self.stdout.write(
            self.style.SUCCESS(f"Trend sources: {created} created, {updated} refreshed.")
        )
