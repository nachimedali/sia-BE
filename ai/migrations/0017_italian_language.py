"""Adds Italian as a content language (L-6, amended 2026-10-12).

0011 seeded the languages that existed then, and a migration that has run does
not run again — so a database seeded before this needs the row from here.
`get_or_create` only, like every seed (`common.seeding`): an edited row is never
overwritten, and a second run adds nothing. Off under test settings.
"""

from django.db import migrations

from ai.creative_seed import CATALOG
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    position, spec = next(
        (i, row) for i, row in enumerate(CATALOG["language"]) if row["key"] == "it"
    )
    defaults = {**spec, "sort_order": position * 10, "is_active": True}
    key = defaults.pop("key")
    apps.get_model("ai", "CreativeOption").objects.get_or_create(
        kind="language", key=key, defaults=defaults
    )


class Migration(migrations.Migration):
    dependencies = [("ai", "0016_generationvariant_reference_closest_and_more")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
