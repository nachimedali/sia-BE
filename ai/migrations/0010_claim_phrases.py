"""Gives each seeded claim the phrases a product-page import matches (S1).

Adds `metadata.phrases` only where a row has none, so phrases an operator wrote
in admin are never overwritten — the same rule as every seed in this project.
"""

from django.db import migrations

from common.seeding import seeding_enabled

from ai.creative_seed import CLAIM_PHRASES


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for row in option_model.objects.filter(kind="claim", key__in=list(CLAIM_PHRASES)):
        metadata = dict(row.metadata or {})
        if metadata.get("phrases"):
            continue
        metadata["phrases"] = CLAIM_PHRASES[row.key]
        row.metadata = metadata
        row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0009_revise_reason")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
