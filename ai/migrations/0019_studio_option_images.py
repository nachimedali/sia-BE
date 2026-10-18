"""Gives the Studio's look options their thumbnail (`metadata.image`).

0011 seeded the rows before the pictures existed and a migration that has run
does not run again, so a database seeded before this has none. Only a row with
no `image` is touched — a picture an operator set in admin is left alone — and
only the options the seed lists (`creative_seed.STUDIO_IMAGES`). Off under test
settings.
"""

from django.db import migrations

from ai.creative_seed import STUDIO_IMAGES, studio_image
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, keys in STUDIO_IMAGES.items():
        for row in option_model.objects.filter(kind=kind, key__in=keys):
            if row.metadata.get("image"):
                continue
            row.metadata = {**row.metadata, "image": studio_image(kind, row.key)}
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0018_drop_tunisian_scenes")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
