"""S17-36: the customer quote template (`proof-card`).

Quotes a review the brand recorded with the customer's consent; never shown
without it. `get_or_create` only; off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for position, spec in enumerate(TEMPLATES):
        if spec["key"] != "proof-card":
            continue
        defaults = {**spec, "sort_order": position * 10, "is_active": True}
        key = defaults.pop("key")
        defaults["translations"] = translations_for("ad_template", key)
        option_model.objects.get_or_create(kind="ad_template", key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0044_videorender_checks")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
