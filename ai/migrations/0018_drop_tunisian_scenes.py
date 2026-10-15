"""Removes the Tunisian scenes and the preset built on one from the catalog.

0011 seeded them and a migration that has run does not run again, so a database
seeded before this still holds the rows. Only rows still carrying the seed marker
are removed — an operator's own row that happens to share a key is left alone —
and products drop the keys, since an unknown scene would otherwise fail the
catalog check on their next save. The Ramadan preset keeps existing and is
re-pointed at a scene that remains. Off under test settings.
"""

from django.db import migrations

from common.seeding import seeding_enabled

SCENES = ("sidi-bou-said", "medina-souk", "dar-courtyard", "tunis-rooftop")
PRESETS = ("medina-story",)


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seeded = [r for r in option_model.objects.filter(kind="scene", key__in=SCENES)]
    option_model.objects.filter(pk__in=[r.pk for r in seeded if r.metadata.get("seed")]).delete()
    option_model.objects.filter(kind="preset", key__in=PRESETS).delete()
    for row in option_model.objects.filter(kind="preset"):
        values = dict(row.metadata.get("values") or {})
        if values.get("scene") in SCENES:
            values["scene"] = "garden-terrace"
            row.metadata = {**row.metadata, "values": values}
            row.save(update_fields=["metadata"])

    product_model = apps.get_model("products", "Product")
    for product in product_model.objects.all().iterator():
        scenes = [s for s in product.scenes or [] if s not in SCENES]
        if scenes != (product.scenes or []):
            product.scenes = scenes
            product.save(update_fields=["scenes"])


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0017_italian_language"),
        ("products", "0004_product_brief"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
