"""S17-34: angles and hook styles as catalogue rows.

Adds the `angle` and `hook_family` kinds and seeds them with their French and
Italian names; each row's `prompt_fragment` is the instruction the script
writer reads. `get_or_create` only; off under test settings, like every seed.
"""

from django.db import migrations, models

from ai.ad_template_seed import ANGLES, HOOK_FAMILIES
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, rows in (("hook_family", HOOK_FAMILIES), ("angle", ANGLES)):
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            defaults["translations"] = translations_for(kind, key)
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0042_graphic_templates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="creativeoption",
            name="kind",
            field=models.CharField(
                choices=[
                    ("scene", "Scene"),
                    ("light", "Light"),
                    ("camera", "Camera angle"),
                    ("cast", "Who is in the frame"),
                    ("vibe", "Cast vibe"),
                    ("mood", "Mood"),
                    ("palette", "Palette"),
                    ("language", "Content language"),
                    ("cta", "Call to action"),
                    ("format", "Format"),
                    ("tempo", "Tempo (creativity)"),
                    ("dynamics", "Dynamics (colour intensity)"),
                    ("tone", "Voice"),
                    ("toggle", "On/off option"),
                    ("preset", "Preset"),
                    ("quick_tag", "Brief quick tag"),
                    ("audience", "Product audience"),
                    ("tone_preset", "Voice preset"),
                    ("claim", "Claim that needs proof"),
                    ("shot_tag", "Photo shot type"),
                    ("aspect", "Image aspect"),
                    ("suggestion", "Suggested entry"),
                    ("revise_reason", "Why regenerate"),
                    ("video_length", "Video length"),
                    ("motion", "Camera motion"),
                    ("video_aspect", "Video frame"),
                    ("reel_style", "Reel style"),
                    ("music", "Music bed"),
                    ("video_extra", "Video surcharge"),
                    ("plan_price", "Campaign plan price"),
                    ("ad_template", "Video ad template"),
                    ("avatar_trait", "Avatar trait"),
                    ("place", "Template place"),
                    ("angle", "Ad angle"),
                    ("hook_family", "Hook style"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
