"""Discovery, $29: a paid first step below Essential (2026-10-27, owner;
`zeely-comparison.md`, L-4 amended in BUILD-PLAN.md).

A fresh database gets it from `0022`, which reads `seed_plans.PACKAGES`. A
database that ran `0022` before Discovery existed gets it here —
`get_or_create`, never overwriting a row an operator has edited. Only when the
row is created here do Essential, Growth and Scale move one place down the
list, and each only while it still holds the order `0022` gave it. Off under
test settings, like every seed.
"""

from django.db import migrations

from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled

# (code, order 0022 gave it, order it takes below Discovery), dearest first.
SHIFTS = (("scale", 13, 14), ("growth", 12, 13), ("essential", 11, 12))


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    currency_model = apps.get_model("billing", "Currency")
    plan_model = apps.get_model("billing", "Plan")
    plan_price_model = apps.get_model("billing", "PlanPrice")

    spec = next(spec for spec in PACKAGES if spec["code"] == "discovery")
    fields = {k: v for k, v in spec.items() if k not in ("code", "is_default")}
    fields["is_recommended"] = False
    plan, created = plan_model.objects.get_or_create(code="discovery", defaults=fields)
    currency = currency_model.objects.get_or_create(
        code=plan.currency.upper(), defaults={"name": plan.currency.upper(), "symbol": "$"}
    )[0]
    plan_price_model.objects.get_or_create(
        plan=plan,
        currency=currency,
        defaults={
            "monthly_cents": plan.price_monthly_cents,
            "annual_cents": plan.price_annual_cents,
            "per_workspace_cents": plan.price_per_workspace_cents,
            "stripe_price_id_monthly": "",
            "stripe_price_id_annual": "",
            "is_default": True,
        },
    )
    if created:
        for code, seeded, below in SHIFTS:
            plan_model.objects.filter(code=code, sort_order=seeded).update(sort_order=below)


class Migration(migrations.Migration):
    dependencies = [("billing", "0025_plan_page_features")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
