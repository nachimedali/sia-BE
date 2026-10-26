"""Whole pages per plan: `campaigns`, `templates`, `analytics` (2026-10-26).

A feature key a plan does not carry reads as "not in the plan", so every
existing row has to state them or it loses pages it had. Each key is added only
where the row does not carry it yet, so an operator's value is never
overwritten: Spark's comes from `seed_plans` (off), every other row keeps the
pages it already opened (on), admin-made plans included. Not gated on seeding:
this preserves existing rows rather than seeding new ones.
"""

from django.db import migrations

from billing.management.commands.seed_plans import PACKAGES, PLANS

KEYS = ("campaigns", "templates", "analytics")


def forwards(apps, schema_editor):
    specs = {spec["code"]: spec["features"] for spec in [*PLANS, *PACKAGES]}
    plan_model = apps.get_model("billing", "Plan")
    for plan in plan_model.objects.all():
        seeded = specs.get(plan.code, {})
        missing = {key: seeded.get(key, True) for key in KEYS if key not in plan.features}
        if missing:
            plan.features = {**plan.features, **missing}
            plan.save(update_fields=["features", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0024_ad_template_limits")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
