"""Re-prices the two clip lengths for a credit worth $0.10 (pricing.audit.md §8).

A render debits `metadata.credits` of its length row. At 6 and 10 credits a
clip sold below what one render costs on any current video model. Only a row
still at its seeded price is touched — a price an operator set in admin stays.
Off under test settings.
"""

from django.db import migrations

from common.seeding import seeding_enabled

REPRICED = {"clip-5": (6, 25), "clip-10": (10, 50)}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for key, (old, new) in REPRICED.items():
        for row in option_model.objects.filter(kind="video_length", key=key):
            if (row.metadata or {}).get("credits") != old:
                continue
            row.metadata = {**row.metadata, "credits": new}
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0021_seed_model_routes")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
