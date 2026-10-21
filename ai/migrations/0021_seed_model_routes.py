"""Seeds the model routes (`ai/route_seed.py`, pricing.plan.md Phase 4).

`get_or_create` on `(task, plan)`: a route an operator has edited is never
overwritten, and a package that does not exist on this database (an operator
deleted or never seeded it) is skipped rather than failed. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.route_seed import FIELDS, ROUTES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    route_model = apps.get_model("ai", "AIModelRoute")
    plan_model = apps.get_model("billing", "Plan")
    for spec in ROUTES:
        plan = None
        if spec["plan"]:
            plan = plan_model.objects.filter(code=spec["plan"]).first()
            if plan is None:
                continue
        route_model.objects.get_or_create(
            task=spec["task"],
            plan=plan,
            defaults={key: spec[key] for key in FIELDS},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0020_model_routes"),
        ("billing", "0022_packages_2026_10"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
