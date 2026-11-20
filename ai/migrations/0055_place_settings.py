"""S17-43: each catalogue place says what kind of place it is (`setting`) and
whether it is outdoors — a brand's own place fits the templates whose places
share its setting. Fills only what a row does not say yet: an admin's edit is
never overwritten. Reversing leaves the keys (they are harmless).

And the captures' finish stops naming a light source in each template's first
frame prompt ("one soft light from a window or lamp" on a beach, "controlled
studio light" on a terrace): replaced only where the seeded words are still
there, so an admin's own wording stays."""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.db import migrations

from ai.ad_template_seed import OUTDOOR_PLACES, PLACE_SETTINGS
from ai.creative_i18n import translations_for
from ai.creative_seed import SEED_VERSION


#: (seeded words, S17-43 words) in `ad_template` first-frame prompts.
FINISH_WORDS = (
    (
        "one soft light from a window or lamp to one side, normal exposure, natural matte "
        "skin; authentic and unpolished",
        "the light the place already has, normal exposure, natural matte skin; authentic "
        "and unpolished",
    ),
    ("controlled studio light, the product", "controlled, even light, the product"),
)


#: (seeded words, S17-43 words) in shots the video model is sent: it cannot know
#: the call to action is the composer's caption, and drew one.
BEAT_WORDS = (
    (
        "{product} holds still under the call to action.",
        "{product} holds still, the top third of the frame left clear.",
    ),
    (
        "all of them, still, under the call to action.",
        "all of them, still, the top third of the frame left clear.",
    ),
)


def forwards(apps: Any, schema_editor: Any) -> None:
    CreativeOption = apps.get_model("ai", "CreativeOption")
    for row in CreativeOption.objects.filter(kind="ad_template"):
        meta = dict(row.metadata or {})
        prompt = str(meta.get("keyframe_prompt") or "")
        new = prompt
        for old, words in FINISH_WORDS:
            new = new.replace(old, words)
        direction = json.dumps(meta.get("direction") or {})
        fixed = direction
        for old, words in BEAT_WORDS:
            fixed = fixed.replace(json.dumps(old)[1:-1], json.dumps(words)[1:-1])
        if new != prompt or fixed != direction:
            meta["keyframe_prompt"] = new
            if fixed != direction:
                meta["direction"] = json.loads(fixed)
            row.metadata = meta
            row.save(update_fields=["metadata"])
    # The "Has printed text" photo tag, for databases seeded before it.
    if getattr(settings, "SEED_CATALOG_ON_MIGRATE", True):
        CreativeOption.objects.get_or_create(
            kind="shot_tag",
            key="with-text",
            defaults={
                "label": "Has printed text",
                "translations": translations_for("shot_tag", "with-text"),
                "metadata": {"seed": SEED_VERSION},
                "sort_order": 100,
            },
        )
    for row in CreativeOption.objects.filter(kind="place"):
        meta = dict(row.metadata or {})
        changed = False
        if "setting" not in meta:
            meta["setting"] = PLACE_SETTINGS.get(row.key, row.key)
            changed = True
        if "outdoor" not in meta:
            meta["outdoor"] = row.key in OUTDOOR_PLACES
            changed = True
        if changed:
            row.metadata = meta
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0054_videorender_beat_frames")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
