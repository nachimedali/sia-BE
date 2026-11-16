"""S19-12: the 26 Pro scenes (`ai/scene_seed_s19.py`), with French and Italian.

`get_or_create` on the key, so a row an operator has edited is never
overwritten. Each is `premium` and `pro`: the gallery shows it, and a render
accepts it, only with `pro_rendering_s19` on.

Off under test settings, like every seed.
"""

from django.db import migrations

from ai.creative_i18n import translations_for
from ai.scene_seed_s19 import SCENES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for spec in SCENES:
        defaults = {
            **{k: v for k, v in spec.items() if k != "key"},
            "is_active": True,
            "translations": translations_for("ad_template", spec["key"]),
        }
        option_model.objects.get_or_create(kind="ad_template", key=spec["key"], defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0052_video_look")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
