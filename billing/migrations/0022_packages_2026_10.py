"""The packages of 2026-10-21: Spark, Essential, Growth, Scale
(pricing.audit.md §7; L-4 amended by change request in BUILD-PLAN.md).

`billing/0018` ran before these existed and a migration that has run does not
run again, so they are created here — `get_or_create` from
`seed_plans.PACKAGES`, never overwriting a row an operator has edited. Off
under test settings, like every seed; the suite builds its own rows.

Alongside, and each only while the row is still as seeded:

- Spark becomes the default plan (it was the row coded `free`, `0021`);
- the legacy public rows (Trial, Pro, Advanced) leave the public list — their
  subscribers keep them, nobody new can pick them;
- the recommended badge moves from Pro to Growth;
- the `credits-100` pack replaces `credits-500` (a credit is worth $0.10 now).

Stripe price ids are not seeded: they are pasted into each plan's price row in
admin once the products exist in Stripe (pricing.plan.md, Phase 1).
"""

from django.db import migrations

from billing.management.commands.seed_packs import PACKS
from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled

RETIRED_PLANS = ("trial", "pro", "advanced")
RETIRED_PACKS = ("credits-500",)
NEW_PACKS = ("credits-100",)


def _currency(model, code):
    return model.objects.get_or_create(
        code=code.upper(), defaults={"name": code.upper(), "symbol": "$"}
    )[0]


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    currency_model = apps.get_model("billing", "Currency")
    plan_model = apps.get_model("billing", "Plan")
    plan_price_model = apps.get_model("billing", "PlanPrice")
    pack_model = apps.get_model("billing", "Pack")
    pack_price_model = apps.get_model("billing", "PackPrice")

    created_default = None
    for spec in PACKAGES:
        fields = {k: v for k, v in spec.items() if k not in ("code", "is_default")}
        fields["is_recommended"] = False
        plan, created = plan_model.objects.get_or_create(code=spec["code"], defaults=fields)
        plan_price_model.objects.get_or_create(
            plan=plan,
            currency=_currency(currency_model, plan.currency),
            defaults={
                "monthly_cents": plan.price_monthly_cents,
                "annual_cents": plan.price_annual_cents,
                "per_workspace_cents": plan.price_per_workspace_cents,
                "stripe_price_id_monthly": "",
                "stripe_price_id_annual": "",
                "is_default": True,
            },
        )
        if created and spec.get("is_default"):
            created_default = plan

    # The floor moves to Spark only if it still sits where 0021 put it.
    if created_default is not None:
        current = plan_model.objects.filter(is_default=True).first()
        if current is None or current.code == "free":
            plan_model.objects.filter(is_default=True).update(is_default=False)
            plan_model.objects.filter(pk=created_default.pk).update(is_default=True)

    plan_model.objects.filter(code__in=RETIRED_PLANS, is_public=True).update(is_public=False)
    if plan_model.objects.filter(code="pro", is_recommended=True).exists():
        plan_model.objects.filter(is_recommended=True).update(is_recommended=False)
        plan_model.objects.filter(code="growth").update(is_recommended=True)

    for spec in PACKS:
        if spec["code"] not in NEW_PACKS:
            continue
        pack, _ = pack_model.objects.get_or_create(
            code=spec["code"], defaults={k: v for k, v in spec.items() if k != "code"}
        )
        pack_price_model.objects.get_or_create(
            pack=pack,
            currency=_currency(currency_model, pack.currency),
            defaults={"amount_cents": pack.price_cents, "stripe_price_id": "", "is_default": True},
        )
    pack_model.objects.filter(code__in=RETIRED_PACKS, is_public=True).update(is_public=False)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0021_plan_is_default"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
