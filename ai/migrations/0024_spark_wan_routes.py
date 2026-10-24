"""Spark's video routes: Wan 3.0 at 720p, no fallback (`ai/route_seed.py`).

`0021` seeded the routes before Spark had video, and a migration that has run
does not run again. `get_or_create` on `(task, plan)`, so an operator's edit
is never overwritten. Off under test settings, like every seed.
"""

from django.db import migrations

from ai.route_seed import FIELDS, ROUTES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    route_model = apps.get_model("ai", "AIModelRoute")
    plan = apps.get_model("billing", "Plan").objects.filter(code="spark").first()
    if plan is None:
        return
    for spec in ROUTES:
        if spec["plan"] == "spark" and spec["task"] in ("video_start", "video_end"):
            route_model.objects.get_or_create(
                task=spec["task"], plan=plan, defaults={key: spec[key] for key in FIELDS}
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0023_videorender_free"),
        ("billing", "0023_spark_free_videos"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
