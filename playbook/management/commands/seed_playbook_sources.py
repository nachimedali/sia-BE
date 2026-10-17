"""Seeds the Playbook's sources for every market in `PLAYBOOK_REGIONS`.

Per market: Google Trends' daily list, YouTube's most-popular chart, and one
YouTube search per root category. Nothing is fetched here — sources say where
to look; `collect_playbook` looks.

The search terms and cadences are a starting point an operator retunes in
admin. They respect YouTube's quota: a search is 100 units, the default daily
quota is 10,000, and twelve-hourly searches across the root categories plus an
hourly chart stay well inside it.

Idempotent and non-destructive: `get_or_create` on the source's identity, so an
edited row is never overwritten (`--refresh` resets the defaults on purpose).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

#: Root category slug -> what to search YouTube for. A root without an entry
#: searches for its own name.
ROOT_TERMS: dict[str, list[str]] = {
    "home-lifestyle": ["home decor"],
    "fashion-apparel": ["outfit ideas"],
    "food-beverage": ["recipe"],
    "beauty-wellness": ["skincare routine"],
    "sports-outdoors": ["workout"],
}

MARKET_SOURCES: list[dict[str, Any]] = [
    {
        "provider": "google_trends",
        "label": "Google Trends · daily searches",
        "refresh": 60,
        "config": {},
    },
    {
        "provider": "youtube",
        "label": "YouTube · most popular",
        "refresh": 120,
        "config": {"chart": True, "page_size": 25, "run_quota_units": 1},
    },
]
SEARCH_REFRESH_MINUTES = 720


def seed(
    source_model: Any, category_model: Any, regions: list[str], *, refresh: bool = False
) -> int:
    """Shared by the command and the data migration, which passes historical
    models — so this function names no model class of its own."""
    created = 0

    def put(
        region: str, category: Any, provider: str, label: str, minutes: int, config: dict[str, Any]
    ) -> None:
        nonlocal created
        defaults = {"label": label, "refresh_minutes": minutes, "config": config, "is_active": True}
        row, made = source_model.objects.get_or_create(
            provider=provider, region=region, category=category, defaults=defaults
        )
        created += made
        if refresh and not made:
            for name, value in defaults.items():
                setattr(row, name, value)
            row.save()

    roots = list(category_model.objects.filter(parent__isnull=True, is_active=True))
    for region in [r.upper() for r in regions]:
        for spec in MARKET_SOURCES:
            put(
                region, None, spec["provider"], spec["label"], spec["refresh"], dict(spec["config"])
            )
        for root in roots:
            put(
                region,
                root,
                "youtube",
                f"YouTube · {root.name}",
                SEARCH_REFRESH_MINUTES,
                {
                    "search_terms": ROOT_TERMS.get(root.slug, [root.name]),
                    "page_size": 25,
                    "run_quota_units": 101,
                },
            )
    return created


class Command(BaseCommand):
    help = "Seeds the Playbook's sources (Google Trends and YouTube) per market."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--refresh", action="store_true", help="Reset edited rows too.")

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        from categories.models import Category
        from playbook.models import PlaybookSource

        created = seed(
            PlaybookSource, Category, list(settings.PLAYBOOK_REGIONS), refresh=options["refresh"]
        )
        self.stdout.write(self.style.SUCCESS(f"Playbook sources: {created} created."))
