import secrets

from django.db import migrations, models

import products.models


def backfill(apps, schema_editor):
    Product = apps.get_model("products", "Product")
    taken: set[str] = set()
    for product in Product.objects.filter(ref__isnull=True).only("pk").iterator():
        ref = secrets.token_hex(4)
        while ref in taken:
            ref = secrets.token_hex(4)
        taken.add(ref)
        Product.objects.filter(pk=product.pk).update(ref=ref)


class Migration(migrations.Migration):
    dependencies = [("products", "0006_product_offer_kind")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="ref",
            field=models.CharField(max_length=8, null=True, editable=False),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="product",
            name="ref",
            field=models.CharField(
                max_length=8,
                unique=True,
                default=products.models.new_product_ref,
                editable=False,
            ),
        ),
    ]
