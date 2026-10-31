"""Discovery's video routes: 720p, as on Essential (`ai/route_seed.py`).

Without them Discovery would fall to the default rows and render at 1080p,
about four times the cost its price was set on. `get_or_create` on
`(task, plan)`, so an operator's edit is never overwritten. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.route_seed import FIELDS, ROUTES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    route_model = apps.get_model("ai", "AIModelRoute")
    plan = apps.get_model("billing", "Plan").objects.filter(code="discovery").first()
    if plan is None:
        return
    for spec in ROUTES:
        if spec["plan"] == "discovery":
            route_model.objects.get_or_create(
                task=spec["task"], plan=plan, defaults={key: spec[key] for key in FIELDS}
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0027_seed_multi_shot_templates"),
        ("billing", "0026_discovery_package"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
