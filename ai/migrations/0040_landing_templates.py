"""S17-31: the hero templates land on their first frame.

`hero-orbit`, `levitate` and `liquid-splash` are filmed towards the approved
still, which the clip ends on (`land: true`, the `video_end` route). Each row
takes the new motion and the flag **only where its motion is still the old
seed's** — an operator's own motion is never overwritten. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES
from common.seeding import seeding_enabled

OLD_MOTION = {
    "hero-orbit": (
        "The camera orbits slowly halfway round the product, then pushes in to a macro detail; "
        "perfectly smooth, the product never moves or changes. The product never changes shape, "
        "label or colour."
    ),
    "levitate": (
        "The product lifts off the plinth and floats, turning slowly in the air. The product "
        "never changes shape, label or colour."
    ),
    "liquid-splash": (
        "A splash of liquid rises around the product in slow motion, then settles. The product "
        "never changes shape, label or colour."
    ),
}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seed = {spec["key"]: spec["metadata"] for spec in TEMPLATES}
    for key, old in OLD_MOTION.items():
        for row in option_model.objects.filter(kind="ad_template", key=key):
            if row.metadata.get("motion_prompt") != old:
                continue
            row.metadata = {
                **row.metadata,
                "motion_prompt": seed[key]["motion_prompt"],
                "land": True,
            }
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0039_avatar_identity_traits")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
