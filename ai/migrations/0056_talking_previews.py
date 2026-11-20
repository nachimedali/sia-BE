"""S17-17: preview clips for the three talking templates (delivered 2026-11-20).

Same as `0046_template_previews.py`, for this batch's keys: each row takes its
clip and first-frame paths **only where it names no clip**. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import preview_of
from common.seeding import seeding_enabled

KEYS = ("founder-story", "three-reasons", "comment-reply")


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for row in option_model.objects.filter(kind="ad_template", key__in=KEYS):
        meta = dict(row.metadata or {})
        if meta.get("preview"):
            continue
        row.metadata = {**meta, **preview_of(row.key)}
        row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0055_place_settings")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
