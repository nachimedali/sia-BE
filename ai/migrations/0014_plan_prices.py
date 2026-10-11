"""The `plan_price` kind and its seeded rows (steps-plan S5, `ai/plan_seed.py`).

`get_or_create` only, like every seed (`common.seeding`): an edited price is
never overwritten, and a second run adds nothing. Off under test settings.
"""

from django.db import migrations, models

from ai.plan_seed import PLAN_CATALOG
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for kind, rows in PLAN_CATALOG.items():
        for position, spec in enumerate(rows):
            defaults = {**spec, "sort_order": position * 10, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind=kind, key=key, defaults=defaults)


class Migration(migrations.Migration):

    dependencies = [
        ('ai', '0013_video_catalog'),
    ]

    operations = [
        migrations.AlterField(
            model_name='creativeoption',
            name='kind',
            field=models.CharField(choices=[('scene', 'Scene'), ('light', 'Light'), ('camera', 'Camera angle'), ('cast', 'Who is in the frame'), ('vibe', 'Cast vibe'), ('mood', 'Mood'), ('palette', 'Palette'), ('language', 'Content language'), ('cta', 'Call to action'), ('format', 'Format'), ('tempo', 'Tempo (creativity)'), ('dynamics', 'Dynamics (colour intensity)'), ('tone', 'Voice'), ('toggle', 'On/off option'), ('preset', 'Preset'), ('quick_tag', 'Brief quick tag'), ('audience', 'Product audience'), ('tone_preset', 'Voice preset'), ('claim', 'Claim that needs proof'), ('shot_tag', 'Photo shot type'), ('aspect', 'Image aspect'), ('suggestion', 'Suggested entry'), ('revise_reason', 'Why regenerate'), ('video_length', 'Video length'), ('motion', 'Camera motion'), ('video_aspect', 'Video frame'), ('reel_style', 'Reel style'), ('music', 'Music bed'), ('video_extra', 'Video surcharge'), ('plan_price', 'Campaign plan price')], max_length=16),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
