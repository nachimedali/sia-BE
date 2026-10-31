"""Seeds the ad templates added 2026-10-27, second pass: the formats the ad
references rank highest (coreyhaines31/marketingskills `ad-creative`,
gooseworks-ai/goose-skills `render-*`) — eight that render today and six
talking formats shown as coming — and the `promo` rail.

`get_or_create`: an operator's edit is never overwritten. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from common.seeding import seeding_enabled

KEYS = (
    "reaction-reveal",
    "reaction-demo",
    "value-stack",
    "offer-drop",
    "lineup",
    "hero-orbit",
    "macro-sizzle",
    "swatch-test",
    "expert-take",
    "investigation",
    "underdog",
    "green-screen",
    "comment-reply",
    "car-chat",
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
    dependencies = [("ai", "0029_seed_more_ad_templates")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
