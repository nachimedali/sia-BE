"""S20-01: the storyboard's ready-made scenes (`ai/story_scene_seed.py`), with
French and Italian, as a new creative kind.

`get_or_create` on the key, so a row an operator has edited is never
overwritten. Served only with `studio_storyboard_s20` on. Off under test
settings, like every seed.
"""

from django.db import migrations, models

from ai.story_scene_seed import SCENES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for position, spec in enumerate(SCENES):
        defaults = {
            **{k: v for k, v in spec.items() if k != "key"},
            "sort_order": position * 10,
            "is_active": True,
        }
        option_model.objects.get_or_create(kind="story_scene", key=spec["key"], defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0058_story_scene_kind")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
