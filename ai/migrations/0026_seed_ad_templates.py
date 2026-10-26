"""Seeds S17: the ad templates and avatar traits (`ai/ad_template_seed.py`),
the 15-second clip, a unit weight on every video length, a reading rate on the
content languages, the avatar portrait price and its model route.

`get_or_create` everywhere, and a value is only *added* to a row's metadata
where the key is missing — an operator's edit is never overwritten. Off under
test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import AD_TEMPLATE_CATALOG, LANGUAGE_WPS, LENGTH_UNITS
from ai.route_seed import FIELDS, ROUTES
from ai.video_seed import LENGTHS
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, rows in AD_TEMPLATE_CATALOG.items():
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)

    clip_15 = next(row for row in LENGTHS if row["key"] == "clip-15")
    defaults = {**clip_15, "sort_order": 15, "is_active": True}
    defaults.pop("key")
    option_model.objects.get_or_create(kind="video_length", key="clip-15", defaults=defaults)

    for key, units in LENGTH_UNITS.items():
        for row in option_model.objects.filter(kind="video_length", key=key):
            if "units" not in (row.metadata or {}):
                row.metadata = {**row.metadata, "units": units}
                row.save(update_fields=["metadata"])
    for key, wps in LANGUAGE_WPS.items():
        for row in option_model.objects.filter(kind="language", key=key):
            if "wps" not in (row.metadata or {}):
                row.metadata = {**row.metadata, "wps": wps}
                row.save(update_fields=["metadata"])

    apps.get_model("ai", "GenerationCost").objects.get_or_create(
        kind="IMAGE", mode="AVATAR", provider="", model="", defaults={"credits": 12}
    )

    route_model = apps.get_model("ai", "AIModelRoute")
    for spec in ROUTES:
        if spec["task"] == "avatar_portrait" and spec["plan"] is None:
            route_model.objects.get_or_create(
                task=spec["task"], plan=None, defaults={key: spec[key] for key in FIELDS}
            )


class Migration(migrations.Migration):
    dependencies = [("ai", "0025_video_units_and_templates")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
