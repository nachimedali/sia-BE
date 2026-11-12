"""S17-39: template preview clips.

Each template with a placeholder clip (`ai.ad_template_seed.PREVIEWS`) takes its
clip and first-frame paths **only where the row names none** — an operator's
own clip is never touched. Off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import PREVIEWS, preview_of
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for row in option_model.objects.filter(kind="ad_template", key__in=list(PREVIEWS)):
        meta = dict(row.metadata or {})
        if meta.get("preview"):
            continue
        row.metadata = {**meta, **preview_of(row.key)}
        row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0045_proof_card")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
