"""Seeds the Motion step's catalogue (steps-plan S3, `ai/video_seed.py`).

`get_or_create` only, like every seed (`common.seeding`): an edited row is never
overwritten, and a second run adds nothing. Off under test settings.
"""

from django.db import migrations

from ai.video_seed import VIDEO_CATALOG
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, rows in VIDEO_CATALOG.items():
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0012_video_renders")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
