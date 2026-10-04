"""The post editor's "What should change?" chips: a new `CreativeOption` kind,
and its rows seeded into databases that already ran `0008_seed_catalog`.

Same contract as every seed migration: `get_or_create`, never overwriting a row
an operator has edited, and off under test settings (`common.seeding`).
"""

from django.db import migrations, models

from ai.creative_seed import REVISE_REASONS
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for position, spec in enumerate(REVISE_REASONS):
        defaults = {**spec, "sort_order": position * 10, "is_active": True}
        key = defaults.pop("key")
        option_model.objects.get_or_create(kind="revise_reason", key=key, defaults=defaults)


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0008_seed_catalog"),
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
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
