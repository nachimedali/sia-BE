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

from billing.models import Currency, Pack, PackKind, PackPrice

PACKS: list[dict[str, Any]] = [
    {
        # One credit sells for $0.10 since the 2026-10-21 packages
        # (pricing.audit.md §8): task prices are `ceil(cost / 0.03)`, so this
        # pack is what the credit is worth. It replaces `credits-500`.
        "code": "credits-100",
        "display_name": "100 credits",
        "tagline": "Images, captions and video. Bought credits do not expire monthly.",
        "kind": PackKind.CREDITS,
        "units": 100,
        "price_cents": 1000,
        "sort_order": -1,
    },
    {
        "code": "credits-500",
        # Retired: 500 credits for $10 valued a credit at $0.02, below what
        # a video render costs. Kept for its purchase history.
        "is_public": False,
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
    {
        # S17: opens every premium ad template for the organization, for good.
        # The price is a proposal; the owner sets it in admin.
        "code": "templates-premium",
        "display_name": "Premium templates",
        "tagline": "Every premium ad template, for every brand of your organization. Paid once.",
        "kind": PackKind.TEMPLATE,
        "units": 1,
        "price_cents": 1900,
        "sort_order": 2,
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
            # Only the default row. Every other currency is an operator's
            # decision — a seed that invented a euro price would be guessing at
            # a number it has no basis for, and a wrong price is worse than an
            # absent one: the absent one falls back, the wrong one charges.
            currency, _ = Currency.objects.get_or_create(
                code=pack.currency.upper(),
                defaults={"name": pack.currency.upper(), "symbol": "$"},
            )
            PackPrice.objects.update_or_create(
                pack=pack,
                currency=currency,
                defaults={
                    "amount_cents": pack.price_cents,
                    "stripe_price_id": pack.stripe_price_id,
                    "is_default": True,
                },
            )
            verb = "created" if created else "updated"
            self.stdout.write(f"  {verb}: {pack.code} ({pack.display_name})")

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(PACKS)} packs."))
