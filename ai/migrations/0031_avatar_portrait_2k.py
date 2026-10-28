"""Avatar portraits become one 2×2 grid split in four (owner, 2026-10-27): the
default `avatar_portrait` route asks for 2K so each face stays about 1K.
Only a row still holding the seeded 1K moves — an operator's edit stands.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    route_model = apps.get_model("ai", "AIModelRoute")
    for row in route_model.objects.filter(task="avatar_portrait"):
        if (row.params or {}).get("image_size") == "1K":
            row.params = {**row.params, "image_size": "2K"}
            row.save(update_fields=["params"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0030_seed_ad_formats")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
