"""S19-07: places, lights, moods and cast vibes for video (`ai/video_look_seed.py`).

Places gain `ambient` and `depth`, lights `keyword` and `conflicts`, moods
`visible` and `motion`, cast vibes `performance` — each key added only where
the row has none, so an operator's own value stays. `prompt_fragment` is never
touched: the image model reads exactly what it read before. Read only with
`pro_rendering_s19` on.

Off under test settings, like every seed.
"""

from django.db import migrations

from ai.video_look_seed import BY_KIND
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, rows in BY_KIND.items():
        for row in option_model.objects.filter(kind=kind, key__in=list(rows)):
            meta = row.metadata or {}
            added = {key: value for key, value in rows[row.key].items() if key not in meta}
            if added:
                row.metadata = {**meta, **added}
                row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0051_template_directions")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
