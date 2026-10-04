"""Seeds the commercial catalogue: currencies, plans and prepaid packs.

A fresh database used to come up with no plans at all — registration needs the
`trial` plan, so the app was unusable until an operator remembered to run
`seed_currencies seed_plans seed_packs`. The seed now lives in the migration
chain, so `migrate` alone produces a working catalogue.

**Never overwrites.** `get_or_create` on the natural key: a plan an operator has
retuned in admin (I8) is left exactly as it is. The commands remain for the
deliberate overwrite (`seed_plans` re-applies the matrix).

The data is imported from the commands rather than copied so there is one list.
That is the only coupling to application code, and it is why this depends on the
latest billing migration: every column the seed names must exist by then.
"""

from django.db import migrations

from common.seeding import seeding_enabled

from billing.management.commands.seed_currencies import CURRENCIES
from billing.management.commands.seed_packs import PACKS
from billing.management.commands.seed_plans import PLANS


def _currency(model, code, fallback):
    return model.objects.get_or_create(code=code.upper(), defaults=fallback)[0]


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    currency_model = apps.get_model("billing", "Currency")
    plan_model = apps.get_model("billing", "Plan")
    plan_price_model = apps.get_model("billing", "PlanPrice")
    pack_model = apps.get_model("billing", "Pack")
    pack_price_model = apps.get_model("billing", "PackPrice")

    for spec in CURRENCIES:
        currency_model.objects.get_or_create(
            code=spec["code"], defaults={k: v for k, v in spec.items() if k != "code"}
        )

    for spec in PLANS:
        plan, _ = plan_model.objects.get_or_create(
            code=spec["code"], defaults={k: v for k, v in spec.items() if k != "code"}
        )
        # Only the default price, mirroring the plan's own columns. Other
        # currencies are an operator's decision, never a seed's guess.
        plan_price_model.objects.get_or_create(
            plan=plan,
            currency=_currency(
                currency_model, plan.currency, {"name": plan.currency.upper(), "symbol": "$"}
            ),
            defaults={
                "monthly_cents": plan.price_monthly_cents,
                "annual_cents": plan.price_annual_cents,
                "per_workspace_cents": plan.price_per_workspace_cents,
                "stripe_price_id_monthly": plan.stripe_price_id_monthly,
                "stripe_price_id_annual": plan.stripe_price_id_annual,
                "is_default": True,
            },
        )

    for spec in PACKS:
        pack, _ = pack_model.objects.get_or_create(
            code=spec["code"], defaults={k: v for k, v in spec.items() if k != "code"}
        )
        pack_price_model.objects.get_or_create(
            pack=pack,
            currency=_currency(
                currency_model, pack.currency, {"name": pack.currency.upper(), "symbol": "$"}
            ),
            defaults={
                "amount_cents": pack.price_cents,
                "stripe_price_id": pack.stripe_price_id,
                "is_default": True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("billing", "0017_plan_max_tracked_competitors")]

    # No reverse: deleting plans would orphan every subscription that points at
    # one, and the rows may have been edited since.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
