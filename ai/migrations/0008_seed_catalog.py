"""Seeds what generation reads: the credit price table and the Studio's controls.

Without `GenerationCost` rows a generation has no price and without
`CreativeOption` rows the Studio renders no scene, light, camera or cast
choice — the page is empty rather than broken, which is worse. Both now arrive
with `migrate`.

**Never overwrites.** A label, an icon or a credit price an operator retuned in
admin survives; `seed_creative_options --refresh` and `seed_generation_costs`
are the deliberate overwrites. New *choices* added to `ai/creative_seed.py`
later reach existing databases through the command (or a new migration), not by
editing this one — a migration that has run does not run again.
"""

from django.db import migrations

from common.seeding import seeding_enabled

from ai.creative_seed import CATALOG
from ai.management.commands.seed_generation_costs import GENERATION_COSTS


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    cost_model = apps.get_model("ai", "GenerationCost")
    option_model = apps.get_model("ai", "CreativeOption")

    for spec in GENERATION_COSTS:
        cost_model.objects.get_or_create(
            kind=spec["kind"],
            mode=spec["mode"],
            provider=spec["provider"],
            model=spec["model"],
            defaults={"credits": spec["credits"], "is_active": True},
        )

    for kind, rows in CATALOG.items():
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0007_product_brief")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
