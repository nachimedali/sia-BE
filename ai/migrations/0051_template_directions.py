"""S19-04: each template's timed direction (`upgrade_rendering/scenes_revised.md`).

Adds `metadata.direction` — the shot, one camera move per shot, beats as timed
visible actions (or one set per shot), sound and what to avoid — to every
template row that **still holds the seed's motion**. A row whose motion an
operator rewrote in admin is left untouched: its own words keep rendering. A row
that already has a direction keeps it. Read only with `pro_rendering_s19` on.

Off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seed = {spec["key"]: spec["metadata"] for spec in TEMPLATES}
    for row in option_model.objects.filter(kind="ad_template", key__in=list(seed)):
        meta = row.metadata or {}
        if "direction" in meta or meta.get("motion_prompt") != seed[row.key]["motion_prompt"]:
            continue
        row.metadata = {**meta, "direction": seed[row.key]["direction"]}
        row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0050_seed_video_profiles_and_pro_routes")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
