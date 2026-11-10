"""S17-30: the person identity lock's traits.

Seeds the new avatar trait groups — gender, heritage, hair — with their French
and Italian names, and rewrites the outfits (`style-*`) into a whole outfit (top,
bottom, what goes with it) and the upbeat energy into a face that stays relaxed,
**only where a row still holds the old seed's words**: an operator's own words
are never overwritten. `get_or_create` throughout; off under test settings.
"""

from django.db import migrations

from ai.ad_template_seed import AVATAR_TRAITS
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled

NEW_GROUPS = ("gender", "heritage", "hair")
#: The previous seed's words for every trait the new seed rewrote.
OLD_FRAGMENTS = {
    "style-casual": "casual everyday clothes",
    "style-smart": "smart, tailored clothes",
    "style-sporty": "sportswear",
    "style-traditional": "classic, traditional clothes",
    "energy-upbeat": "an upbeat, energetic expression",
}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seed = {row["key"]: row for row in AVATAR_TRAITS}
    for position, spec in enumerate(AVATAR_TRAITS):
        if spec["metadata"]["group"] not in NEW_GROUPS:
            continue
        defaults = {**spec, "sort_order": position * 10, "is_active": True}
        key = defaults.pop("key")
        defaults["translations"] = translations_for("avatar_trait", key)
        option_model.objects.get_or_create(kind="avatar_trait", key=key, defaults=defaults)
    for key, old in OLD_FRAGMENTS.items():
        for row in option_model.objects.filter(kind="avatar_trait", key=key, prompt_fragment=old):
            new = seed[key]
            row.prompt_fragment = new["prompt_fragment"]
            row.metadata = {**(row.metadata or {}), **new["metadata"]}
            row.save(update_fields=["prompt_fragment", "metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0038_template_places")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
