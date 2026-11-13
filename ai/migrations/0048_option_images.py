"""Every Studio choice pictured by its own still.

Places, avatar traits, angles and templates with no clip take their path
(`/studio/<dir>/<key>.webp`) **only where the row names none**, or, for a
place, where it still borrows a scene's still — a picture an operator set in
admin is never touched. Off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import still_of
from common.seeding import seeding_enabled

FOLDERS = {"place": "places", "avatar_trait": "avatar", "angle": "angles"}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for row in option_model.objects.filter(kind__in=[*FOLDERS, "ad_template"]):
        meta = dict(row.metadata or {})
        if row.kind == "ad_template":
            if meta.get("poster"):
                continue
            meta["poster"] = still_of(row.key)
        else:
            image = str(meta.get("image") or "")
            if image and not (row.kind == "place" and image.startswith("/studio/scene/")):
                continue
            meta["image"] = f"/studio/{FOLDERS[row.kind]}/{row.key}.webp"
        row.metadata = meta
        row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0047_text_routes_2026_11")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
