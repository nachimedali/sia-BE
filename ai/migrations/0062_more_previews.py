"""S17-17: preview clips for five more templates (delivered 2026-11-28, `videos.zip`).

Same as `0056_talking_previews.py`, for this batch's keys: each row takes its
clip and first-frame paths **only where it names no clip**. Off under test
settings, like every seed. The eleven clips re-cut in the same batch keep their
paths, so their rows need nothing.
"""

from django.db import migrations

from ai.ad_template_seed import preview_of
from common.seeding import seeding_enabled

KEYS = ("close-detail", "desk-break", "macro-sizzle", "space-tour", "room-reveal")


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
    dependencies = [("ai", "0061_render_layers")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
