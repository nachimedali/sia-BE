"""Seeds the four tools added after 0002 (CTA, rewrite, translate, alt text) on a
database that already ran it. `get_or_create`: an admin edit is never overwritten."""

from django.db import migrations

from common.seeding import seeding_enabled

NEW_TOOLS = [
    {"slug": "cta", "display_name": "CTA writer", "sort_order": 6},
    {"slug": "caption-rewrite", "display_name": "Caption rewriter", "sort_order": 7},
    {"slug": "translate", "display_name": "Translate", "sort_order": 8},
    {"slug": "alt-text", "display_name": "Alt text", "sort_order": 9},
]


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    config_model = apps.get_model("tools", "ToolConfig")
    for spec in NEW_TOOLS:
        config_model.objects.get_or_create(
            slug=spec["slug"], defaults={k: v for k, v in spec.items() if k != "slug"}
        )


class Migration(migrations.Migration):
    dependencies = [("tools", "0003_tool_choices_cta_rewrite_translate_alt")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
