"""S17-38: the three talking templates render — Happy customer (the key stays
`founder-story` for its stills and clips), 3 reasons and Reply to a comment —
at 10 s, saying the person's own lines; and Grok Imagine 1.5 Lite's profile
says it makes sound (OpenRouter `generate_audio: true`, 2026-11-19).

Only a row still in its seeded state is changed (not out, with the old
"arrive with the next update" description): an operator's own edit stays. The
row's preview clip and still (`0056`) are kept. Off under test settings."""

from __future__ import annotations

from typing import Any

from django.db import migrations

from ai.ad_template_seed import TALKING_ON, TEMPLATES
from ai.creative_i18n import translations_for
from common.seeding import seeding_enabled

#: The description every talking template carried while it could not be made.
OLD_DESCRIPTION = (
    "Talking formats need a voice and lip sync, which arrive with the next Studio update."
)
#: What a row takes from the seed; its preview, poster and anything else stay.
FIELDS = (
    "available",
    "lengths",
    "units",
    "beats",
    "script",
    "hooks",
    "script_hint",
    "direction",
)
GROK_LITE = "x-ai/grok-imagine-video-1.5-lite"


def forwards(apps: Any, schema_editor: Any) -> None:
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seeded = {spec["key"]: spec for spec in TEMPLATES if spec["key"] in TALKING_ON}
    for row in option_model.objects.filter(kind="ad_template", key__in=TALKING_ON):
        meta = dict(row.metadata or {})
        if meta.get("available") or row.description != OLD_DESCRIPTION:
            continue  # out already, or an operator's own
        spec = seeded[row.key]
        row.metadata = {
            **meta,
            **{key: spec["metadata"][key] for key in FIELDS if key in spec["metadata"]},
        }
        row.label = spec["label"]
        row.description = spec["description"]
        row.translations = translations_for("ad_template", row.key)
        row.save(update_fields=["metadata", "label", "description", "translations"])
    profile_model = apps.get_model("ai", "VideoModelProfile")
    profile_model.objects.filter(slug=GROK_LITE, audio=False).update(audio=True)


class Migration(migrations.Migration):
    dependencies = [("ai", "0056_talking_previews")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
