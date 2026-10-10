"""Seeds the six quick-tool rows. Never overwrites a price or an enabled flag set in admin."""

from django.db import migrations

from common.seeding import seeding_enabled

from tools.management.commands.seed_tools import TOOLS


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    config_model = apps.get_model("tools", "ToolConfig")
    for spec in TOOLS:
        config_model.objects.get_or_create(
            slug=spec["slug"], defaults={k: v for k, v in spec.items() if k != "slug"}
        )


class Migration(migrations.Migration):
    dependencies = [("tools", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
