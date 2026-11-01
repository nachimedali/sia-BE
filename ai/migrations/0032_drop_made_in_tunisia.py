"""Removes the "Made in Tunisia" claim and its word chip from the catalog.

0011 seeded them and a migration that has run does not run again, so a database
seeded before this still holds the rows. Only rows still carrying the seed marker
are removed — an operator's own row that happens to share a key is left alone —
and products drop the claim key, since an unknown claim would otherwise fail the
catalog check on their next save. A product's own words (`use_words`) are the
brand's and stay. Off under test settings.
"""

from django.db import migrations

from common.seeding import seeding_enabled

ROWS = (("claim", "made-in-tunisia"), ("suggestion", "use_words-made-in-tunisia"))
CLAIM = "made-in-tunisia"


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, key in ROWS:
        for row in option_model.objects.filter(kind=kind, key=key):
            if row.metadata.get("seed"):
                row.delete()

    product_model = apps.get_model("products", "Product")
    for product in product_model.objects.all().iterator():
        claims = [c for c in product.claims or [] if c.get("key") != CLAIM]
        if claims != (product.claims or []):
            product.claims = claims
            product.save(update_fields=["claims"])


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0031_avatar_portrait_2k"),
        ("products", "0004_product_brief"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
