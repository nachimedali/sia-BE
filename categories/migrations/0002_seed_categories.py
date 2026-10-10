"""Seeds the category tree — onboarding's vertical picker and the trend corpus's key.

An empty tree leaves the wizard's category step with nothing to choose and the
trend engine with nothing to partition on. Never overwrites: `get_or_create` on
the slug, so a renamed or deactivated category survives.
"""

from django.db import migrations

from common.seeding import seeding_enabled
from django.utils.text import slugify

from categories.management.commands.seed_categories import TREE


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    category_model = apps.get_model("categories", "Category")
    for root_name, child_names in TREE.items():
        root, _ = category_model.objects.get_or_create(
            slug=slugify(root_name),
            defaults={"name": root_name, "parent": None, "is_active": True},
        )
        for child_name in child_names:
            category_model.objects.get_or_create(
                slug=slugify(f"{root_name} {child_name}"),
                defaults={"name": child_name, "parent": root, "is_active": True},
            )


class Migration(migrations.Migration):
    dependencies = [("categories", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
