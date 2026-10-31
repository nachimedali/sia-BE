"""Seeds the ad templates added 2026-10-27 (owner: more authentic templates,
some premium): fifteen rows closer to the reference scenes in `ad-templates/`,
and the two rails they bring (`use`, `maker`).

`get_or_create`: an operator's edit is never overwritten. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from common.seeding import seeding_enabled

KEYS = (
    "stair-unbox",
    "doorstep-delivery",
    "close-detail",
    "trail-test",
    "street-carry",
    "flat-lay",
    "seaside-wear",
    "beach-day",
    "floor-read",
    "desk-break",
    "made-by-hand",
    "packing-order",
    "room-reveal",
    "gate-app",
    "gym-tablet",
)


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for spec in TEMPLATES:
        if spec["key"] in KEYS:
            defaults = {**spec, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind="ad_template", key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0028_discovery_routes")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
