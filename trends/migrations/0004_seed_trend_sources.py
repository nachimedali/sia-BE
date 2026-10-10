"""Seeds a default trend source for every root category × platform × kind.

Ordered after the category seed — the sources hang off root categories. Never
overwrites: a source an operator repointed at another vendor or re-queried
keeps its edits, since the natural key is `(category, platform, kind, vendor)`.
"""

from django.db import migrations

from common.seeding import seeding_enabled

from trends.management.commands.seed_trend_sources import KIND_VENDORS, PLATFORM_KINDS, ROOT_QUERIES


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    category_model = apps.get_model("categories", "Category")
    source_model = apps.get_model("trends", "TrendSource")
    for root in category_model.objects.filter(parent__isnull=True, is_active=True):
        hints = ROOT_QUERIES.get(root.slug, {})
        for platform, kinds in PLATFORM_KINDS.items():
            for kind in kinds:
                source_model.objects.get_or_create(
                    category=root,
                    workspace=None,  # shared corpus only; a tracked competitor is never seeded
                    platform=platform,
                    kind=kind,
                    vendor=KIND_VENDORS[kind],
                    defaults={"query": {"q": root.name, **hints}, "is_active": True},
                )


class Migration(migrations.Migration):
    dependencies = [
        (
            "trends",
            "0003_remove_trendsource_unique_trend_source_per_category_platform_kind_vendor_and_more",
        ),
        ("categories", "0002_seed_categories"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
