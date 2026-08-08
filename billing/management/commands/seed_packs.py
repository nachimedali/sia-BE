"""Seeds the prepaid packs from design.md §4.3.

Idempotent, like `seed_plans`, and equally provisional: an operator retunes size
or price in admin afterwards (I8). This command only establishes the starting
point.

Video is priced at cost + $2 (D16). At the default 8s / 720p that is $1.20 of
COGS against a $3.20 unit — the same figure the overage table quotes, so buying
a pack and going over cost the user the same thing.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from billing.models import Pack, PackKind

PACKS: list[dict[str, Any]] = [
    {
        "code": "credits-500",
        "display_name": "500 credits",
        "tagline": "Text and image generation. Bought credits do not expire monthly.",
        "kind": PackKind.CREDITS,
        "units": 500,
        "price_cents": 1000,
        "sort_order": 0,
    },
    {
        "code": "videos-4",
        "display_name": "4 videos",
        "tagline": "Prepaid 8-second videos, on top of your monthly allowance.",
        "kind": PackKind.VIDEO,
        "units": 4,
        "price_cents": 1280,
        "sort_order": 1,
    },
]


class Command(BaseCommand):
    help = "Seed or update the prepaid credit and video packs (design.md §4.3)."

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        for spec in PACKS:
            code = spec["code"]
            pack, created = Pack.objects.update_or_create(
                code=code, defaults={k: v for k, v in spec.items() if k != "code"}
            )
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {pack.code} ({pack.display_name})")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PACKS)} packs."))
