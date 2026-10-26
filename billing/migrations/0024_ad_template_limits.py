"""S17 limits on the four packages, and their monthly video units.

`0022` created the packages before these keys existed, and a migration that has
run does not run again. Each feature key is added only where the row does not
carry it yet, and `included_videos` is set only where it still holds the 0 that
`0022` wrote — so an operator's value is never overwritten. Legacy plans are
left alone: off the public list, their keys stay absent and read as "not in
the plan". Off under test settings, like every seed.
"""

from django.db import migrations

from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled

KEYS = (
    "ad_templates",
    "ad_templates_people",
    "ad_templates_talking",
    "max_avatars",
    "avatar_drafts_monthly",
    "ad_rerolls",
    "max_references",
    "real_avatars",
)


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    plan_model = apps.get_model("billing", "Plan")
    for spec in PACKAGES:
        plan = plan_model.objects.filter(code=spec["code"]).first()
        if plan is None:
            continue
        fields = []
        missing = {key: spec["features"][key] for key in KEYS if key not in plan.features}
        if missing:
            plan.features = {**plan.features, **missing}
            fields.append("features")
        if plan.included_videos == 0 and spec["included_videos"]:
            plan.included_videos = spec["included_videos"]
            fields.append("included_videos")
        if fields:
            plan.save(update_fields=[*fields, "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0023_spark_free_videos")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
