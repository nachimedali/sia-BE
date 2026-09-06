"""Gives every existing plan and pack a default price row.

**Expand, not cut-over.** The legacy columns — `Plan.price_monthly_cents`,
`Pack.price_cents` and the Stripe ids beside them — are left exactly as they
are and keep working; `billing.services.pricing` reads a price row when one
exists and falls back to those columns when one does not. So a deployment
part-way through this still quotes a price rather than zero, and rolling back
costs nothing.

The row created carries the item's own `currency` value rather than assuming
USD. That column was decorative until now, but an operator who set it to EUR
meant it, and inventing a dollar price for them would be the one mistake this
migration could make that nobody would notice until an invoice went out.

Reversible: the rows are dropped and the columns were never touched.
"""

from django.db import migrations

#: Matches `pricing.FALLBACK_CURRENCY_CODE`. Duplicated rather than imported —
#: a migration must keep working against the schema and the constants as they
#: were when it ran, and importing from application code couples it to edits
#: made years later.
FALLBACK = "USD"

#: Enough to render a price. The full list is `seed_currencies`, which an
#: operator runs and then edits; this only guarantees the codes already in use
#: resolve to *something*.
KNOWN = {
    "USD": ("US dollar", "$", 2),
    "EUR": ("Euro", "€", 2),
    "GBP": ("Pound sterling", "£", 2),
    "MAD": ("Moroccan dirham", "DH", 2),
    "JPY": ("Japanese yen", "¥", 0),
}


def _currency(model, code):
    code = (code or FALLBACK).upper()
    name, symbol, minor = KNOWN.get(code, (code, "", 2))
    currency, _ = model.objects.get_or_create(
        code=code, defaults={"name": name, "symbol": symbol, "minor_units": minor}
    )
    return currency


def forwards(apps, schema_editor):
    currency_model = apps.get_model("billing", "Currency")
    plan_model = apps.get_model("billing", "Plan")
    plan_price_model = apps.get_model("billing", "PlanPrice")
    pack_model = apps.get_model("billing", "Pack")
    pack_price_model = apps.get_model("billing", "PackPrice")

    for plan in plan_model.objects.all():
        plan_price_model.objects.get_or_create(
            plan=plan,
            currency=_currency(currency_model, plan.currency),
            defaults={
                "monthly_cents": plan.price_monthly_cents,
                "annual_cents": plan.price_annual_cents,
                "per_workspace_cents": plan.price_per_workspace_cents,
                "stripe_price_id_monthly": plan.stripe_price_id_monthly,
                "stripe_price_id_annual": plan.stripe_price_id_annual,
                "is_default": True,
            },
        )

    for pack in pack_model.objects.all():
        pack_price_model.objects.get_or_create(
            pack=pack,
            currency=_currency(currency_model, pack.currency),
            defaults={
                "amount_cents": pack.price_cents,
                "stripe_price_id": pack.stripe_price_id,
                "is_default": True,
            },
        )


def backwards(apps, schema_editor):
    apps.get_model("billing", "PlanPrice").objects.all().delete()
    apps.get_model("billing", "PackPrice").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0014_multi_currency_pricing")]

    operations = [migrations.RunPython(forwards, backwards)]
