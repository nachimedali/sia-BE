"""S17 premium templates (2026-10-27, owner): the `TEMPLATE` pack kind, the
`templates-premium` pack, and three plan keys on the five packages —
`ad_templates_premium`, `ad_avatars_per_ad`, `ad_products_per_ad`.

Each key is added only where the row does not carry it yet, and the pack is
`get_or_create`, so an operator's edit is never overwritten. Its Stripe price
id is pasted in admin. Off under test settings, like every seed.
"""

from django.db import migrations, models

from billing.management.commands.seed_packs import PACKS
from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled

KEYS = ("ad_templates_premium", "ad_avatars_per_ad", "ad_products_per_ad")
PACK = "templates-premium"


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    plan_model = apps.get_model("billing", "Plan")
    for spec in PACKAGES:
        plan = plan_model.objects.filter(code=spec["code"]).first()
        if plan is None:
            continue
        missing = {key: spec["features"][key] for key in KEYS if key not in plan.features}
        if missing:
            plan.features = {**plan.features, **missing}
            plan.save(update_fields=["features", "updated_at"])

    spec = next(spec for spec in PACKS if spec["code"] == PACK)
    pack, _ = apps.get_model("billing", "Pack").objects.get_or_create(
        code=PACK, defaults={k: v for k, v in spec.items() if k != "code"}
    )
    currency, _ = apps.get_model("billing", "Currency").objects.get_or_create(
        code=pack.currency.upper(), defaults={"name": pack.currency.upper(), "symbol": "$"}
    )
    apps.get_model("billing", "PackPrice").objects.get_or_create(
        pack=pack,
        currency=currency,
        defaults={"amount_cents": pack.price_cents, "stripe_price_id": "", "is_default": True},
    )


class Migration(migrations.Migration):
    dependencies = [("billing", "0026_discovery_package")]

    operations = [
        migrations.AlterField(
            model_name="pack",
            name="kind",
            field=models.CharField(
                choices=[
                    ("CREDITS", "Credits"),
                    ("VIDEO", "Video units"),
                    ("TEMPLATE", "Premium templates"),
                ],
                max_length=8,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
