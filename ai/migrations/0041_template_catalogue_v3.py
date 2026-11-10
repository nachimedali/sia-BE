"""S17-32: template catalogue v3.

* Retires `lid-asmr`, `freeze-frame`, `colour-burst` and `kitchen-counter`
  (inactive, never deleted) — only rows carrying the seed marker; an operator's
  own row with the key stays.
* Every template takes its `goal` and `protect` where it has none, and the
  talking formats that cannot be made yet take `hidden` — added keys, so an
  operator's own value is never overwritten.
* Rewrites the pour, before/after and reaction templates and one unboxing hook
  **only where each field still holds the old seed's value**.

Off under test settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import TEMPLATES, _HIDDEN
from common.seeding import seeding_enabled

RETIRED = ("lid-asmr", "freeze-frame", "colour-burst", "kitchen-counter")
_PHONE = (
    "Shot on a phone's main camera: deep focus with no background blur, one soft light from a "
    "window or lamp to one side, normal exposure, natural matte skin; authentic and unpolished, "
    "like a real social video."
)
_PRODUCT = (
    "The {product} is clearly visible and exactly as in the reference photos: same shape, label "
    "and colours."
)
#: The previous seed's words, per template and field, for every field rewritten.
OLD_TEXT: dict[str, dict[str, object]] = {
    "pour-shot": {
        "label": "Pour shot",
        "keyframe_prompt": (
            "Vertical 9:16 photo for a short social ad. A hand holding the product above a glass "
            "or plate, ready to pour. Setting: {location}.{person_optional} "
            f"{_PRODUCT} {_PHONE}"
        ),
        "motion_prompt": (
            "Slow-motion pour, then a slow push-in on the product label. The product never "
            "changes shape, label or colour."
        ),
    },
    "before-after": {
        "label": "Before / after",
        "keyframe_prompt": (
            "Vertical 9:16 photo for a short social ad. Close-up of a person's face or hands, the "
            "product held beside them. Setting: {location}. {person} is in the frame, natural and "
            f"unposed. {_PRODUCT} {_PHONE}"
        ),
        "motion_prompt": (
            "A gentle wipe transition from before to after, then a hold on the result. The "
            "product never changes shape, label or colour."
        ),
        "beats": ["Before", "Apply", "After", "End card"],
        "script": {"hook": "Before.", "body": "After {product}.", "cta": "Try it yourself."},
        "hooks": ["Before.", "Watch the routine.", "Step by step."],
    },
    "reaction-reveal": {
        "keyframe_prompt": (
            "Vertical 9:16 photo for a short social ad. Phone-camera close-up of a person reacting "
            "with genuine surprise, hand half over their mouth, eyes on something just out of "
            "frame. Setting: {location}. {person} is in the frame, natural and unposed. "
            f"{_PRODUCT} {_PHONE}"
        ),
    },
    "slow-unbox": {
        "hooks": ["Wait for it.", "The slowest unboxing you'll watch today.", "Guess what's inside."],
    },
}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for row in option_model.objects.filter(kind="ad_template", key__in=RETIRED, is_active=True):
        if (row.metadata or {}).get("seed"):
            row.is_active = False
            row.save(update_fields=["is_active"])

    seed = {spec["key"]: spec for spec in TEMPLATES}
    for row in option_model.objects.filter(kind="ad_template", key__in=list(seed)):
        spec = seed[row.key]
        new = spec["metadata"]
        meta = dict(row.metadata or {})
        label = row.label
        meta.setdefault("goal", new["goal"])
        meta.setdefault("protect", new["protect"])
        if row.key in _HIDDEN:
            meta.setdefault("hidden", True)
        for field, old in OLD_TEXT.get(row.key, {}).items():
            if field == "label":
                if row.label == old:
                    label = spec["label"]
            elif meta.get(field) == old:
                meta[field] = new[field]
        if meta != row.metadata or label != row.label:
            row.metadata = meta
            row.label = label
            row.save(update_fields=["metadata", "label"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0040_landing_templates")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
