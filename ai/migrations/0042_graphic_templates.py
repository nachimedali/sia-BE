"""S17-33: the graphic formats.

Seeds the offer ad, value prop, variant swipe, mosaic reveal, myth vs fact,
chat reveal, the brand's shop tour and five seasonal offers — reels whose
cards, cutouts, grids and chat bubbles are drawn in code around the product's
real photos. `get_or_create` only: an operator's row with the same key is left
as it is. Off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled

KEYS = (
    "offer-ad",
    "value-prop",
    "variant-swipe",
    "mosaic-reveal",
    "myth-fact",
    "chat-reveal",
    "shop-tour",
    "ramadan-offer",
    "eid-offer",
    "back-to-school",
    "summer-sale",
    "black-friday",
)


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for position, spec in enumerate(TEMPLATES):
        if spec["key"] not in KEYS:
            continue
        defaults = {**spec, "sort_order": position * 10, "is_active": True}
        key = defaults.pop("key")
        defaults["translations"] = translations_for("ad_template", key)
        option_model.objects.get_or_create(kind="ad_template", key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [("ai", "0041_template_catalogue_v3")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
