"""Spark's four free video clips (`free_video_clips`, `free_video_max_seconds`).

`0022` created Spark from `seed_plans.PACKAGES` before these keys existed, and a
migration that has run does not run again. Each key is added only where the
row does not carry it yet, so an operator's value is never overwritten. Off
under test settings, like every seed.
"""

from django.db import migrations

from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled

KEYS = ("free_video_clips", "free_video_max_seconds")


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    plan_model = apps.get_model("billing", "Plan")
    seeded = next(p for p in PACKAGES if p["code"] == "spark")["features"]
    plan = plan_model.objects.filter(code="spark").first()
    if plan is None:
        return
    missing = {key: seeded[key] for key in KEYS if key not in plan.features}
    if missing:
        plan.features = {**plan.features, **missing}
        plan.save(update_fields=["features", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0022_packages_2026_10")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
