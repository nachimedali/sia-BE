"""S17-27: a template's place becomes a catalogue row.

Adds the `place` kind, seeds the places (`ai.ad_template_seed.PLACES`, with their
French and Italian names) and moves each template's list of places to the new
seed **only where it still holds the old seed's list** — an operator's own list
is never touched. The airport template's frame and motion leave the airport the
same way. `get_or_create` throughout; off under test settings, like every seed.
"""

from django.db import migrations, models

from ai.ad_template_seed import PLACES, TEMPLATES
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled

#: The previous seed's places, for every template whose list changed.
OLD_LOCATIONS: dict[str, tuple[str, ...]] = {
    "slow-unbox": ("studio", "kitchen", "rooftop"),
    "gift-reveal": ("studio", "rooftop", "terrace"),
    "lid-asmr": ("studio",),
    "first-look": ("terrace", "kitchen", "studio"),
    "pour-shot": ("kitchen", "terrace", "beach"),
    "hand-turn": ("studio", "kitchen"),
    "morning-ritual": ("kitchen", "terrace"),
    "bag-check": ("rooftop", "terrace", "market"),
    "mirror-try-on": ("studio",),
    "before-after": ("studio",),
    "get-ready": ("studio", "terrace"),
    "tap-through": ("studio", "terrace"),
    "feature-in-15": ("studio", "rooftop"),
    "liquid-splash": ("studio", "beach"),
    "levitate": ("studio", "dunes"),
    "colour-burst": ("studio", "rooftop"),
    "freeze-frame": ("studio",),
    "doorstep-delivery": ("living-room", "loft"),
    "close-detail": ("living-room", "studio"),
    "trail-test": ("trail", "dunes", "beach"),
    "flat-lay": ("kitchen", "office", "studio"),
    "packing-order": ("workshop", "kitchen"),
    "gate-app": ("airport", "street"),
    "desk-break": ("office", "kitchen"),
    "floor-read": ("loft", "living-room"),
    "macro-sizzle": ("kitchen", "studio"),
    "swatch-test": ("studio",),
    "hero-orbit": ("studio",),
    "value-stack": ("studio",),
    "offer-drop": ("studio",),
    "lineup": ("studio",),
    "car-chat": ("street",),
    "made-by-hand": ("workshop", "studio"),
}
#: `gate-app` before it left the airport (departure boards are readable text).
OLD_GATE_APP = {
    "keyframe_prompt": (
        "Vertical 9:16 photo for a short social ad. A hand holding a phone up in an airport "
        "terminal, departure boards soft behind, the app's screen clearly visible. Setting: "
        "{location}.{person_optional} The {product} is clearly visible and exactly as in the "
        "reference photos: same shape, label and colours. A real phone held in a real hand, the "
        "screen facing the camera and evenly lit, deep focus, natural light."
    ),
    "motion_prompt": (
        "Walking handheld feel, the phone held steady while the terminal drifts past behind it; "
        "one thumb tap. The product never changes shape, label or colour."
    ),
}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for position, spec in enumerate(PLACES):
        defaults = {**spec, "sort_order": position * 10, "is_active": True}
        key = defaults.pop("key")
        defaults["translations"] = translations_for("place", key)
        option_model.objects.get_or_create(kind="place", key=key, defaults=defaults)

    seed = {spec["key"]: spec["metadata"] for spec in TEMPLATES}
    for row in option_model.objects.filter(kind="ad_template", key__in=list(seed)):
        meta = dict(row.metadata or {})
        old = OLD_LOCATIONS.get(row.key)
        if old is not None and meta.get("locations") == list(old):
            meta["locations"] = list(seed[row.key]["locations"])
        if row.key == "gate-app":
            for field, text in OLD_GATE_APP.items():
                if meta.get(field) == text:
                    meta[field] = seed[row.key][field]
        if meta != row.metadata:
            row.metadata = meta
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0037_template_capture_and_words"),
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
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
