"""Seeds the currencies the catalogue may be priced in.

Idempotent, like every other seed command: re-running updates in place rather
than duplicating, so it is safe on every deploy.

**This list is a starting point, not the vocabulary.** Adding a currency is an
admin edit — that is the whole reason `Currency` is a table rather than an enum
(I8, Part 7 rule 10). A new market opens on a Tuesday and waiting for a release
to price for it is the wrong constraint.

`minor_units` is the part worth checking before adding one. Most currencies use
two, JPY and KRW use none, and a handful of dinars use three. Getting it wrong
does not corrupt any stored amount — everything is stored in minor units — but
it will render ¥3700 as ¥37.00 on the pricing page.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from billing.models import Currency

CURRENCIES: list[dict[str, Any]] = [
    {"code": "USD", "name": "US dollar", "symbol": "$", "minor_units": 2},
    {"code": "EUR", "name": "Euro", "symbol": "€", "minor_units": 2, "symbol_first": False},
    {"code": "GBP", "name": "Pound sterling", "symbol": "£", "minor_units": 2},
    {
        "code": "MAD",
        "name": "Moroccan dirham",
        "symbol": "DH",
        "minor_units": 2,
        "symbol_first": False,
    },
    {"code": "AED", "name": "UAE dirham", "symbol": "AED", "minor_units": 2},
    {"code": "CAD", "name": "Canadian dollar", "symbol": "CA$", "minor_units": 2},
    {"code": "AUD", "name": "Australian dollar", "symbol": "A$", "minor_units": 2},
    {"code": "CHF", "name": "Swiss franc", "symbol": "CHF", "minor_units": 2},
    # Zero-decimal. The reason `minor_units` exists at all: ¥3700 is ¥3700, not
    # ¥37.00, and treating it as cents undercharges by a hundred.
    {"code": "JPY", "name": "Japanese yen", "symbol": "¥", "minor_units": 0},
]


class Command(BaseCommand):
    help = "Seeds the currencies the catalogue may be priced in."

    def handle(self, *args: Any, **options: Any) -> None:
        for spec in CURRENCIES:
            currency, created = Currency.objects.update_or_create(
                code=spec["code"], defaults={k: v for k, v in spec.items() if k != "code"}
            )
            self.stdout.write(f"  {'created' if created else 'updated'}: {currency.code}")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(CURRENCIES)} currencies."))
