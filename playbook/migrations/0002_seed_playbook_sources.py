"""Seeds the Playbook's sources for every market in `PLAYBOOK_REGIONS`.

`get_or_create` only, through the same function the command uses: an edited
row is never overwritten, and a new default for an existing database is a new
migration or `seed_playbook_sources --refresh`, never an edit to this one.
"""

from django.conf import settings
from django.db import migrations

from common.seeding import seeding_enabled
from playbook.management.commands.seed_playbook_sources import seed


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    seed(
        apps.get_model("playbook", "PlaybookSource"),
        apps.get_model("categories", "Category"),
        list(settings.PLAYBOOK_REGIONS),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("playbook", "0001_initial"),
        ("categories", "0002_seed_categories"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
