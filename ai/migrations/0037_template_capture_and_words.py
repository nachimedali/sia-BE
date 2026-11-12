"""S17-26: existing ad templates take the new capture, negative and words.

`0026`/`0027`/`0029` seeded every template with one first-frame finish
("Authentic, unpolished social-video realism.", wrong for a studio or macro
shot), a negative no service read, and hooks that put an invented experience
or false urgency in an invented person's mouth. A migration that has run does
not run again, so this one moves each field to the current seed **only where
the row still holds the old seed's value** — an operator's edit, field by
field, is never overwritten — and records the capture. Off under test
settings, like every seed.
"""

from django.db import migrations

from ai.ad_template_seed import CAPTURES, TEMPLATES
from common.seeding import seeding_enabled

OLD_FINISH = "Authentic, unpolished social-video realism."
OLD_NEGATIVE = (
    "no resemblance to any real or famous person, no brand logos except the product's own, "
    "no readable text except the product's own, no extra fingers, no warped hands, no product "
    "redesign, no watermark"
)
#: What the previous seed said, per template and field, for every field the
#: new seed rewrote. `hooks` is the whole list; the others are script parts.
OLD_WORDS: dict[str, dict[str, object]] = {
    "first-look": {
        "body": "Opening it for the first time, and it's even better in hand.",
        "hooks": ("First look at {product}.", "I wasn't expecting this.", "Unboxing it live."),
    },
    "market-stroll": {
        "hooks": ("Watch this.", "My market find of the week.", "This is what I came for."),
    },
    "kitchen-counter": {
        "body": "{product} has a place on my counter.",
        "hooks": ("Every morning, same spot.", "The one thing on my counter.", "Kitchen staple."),
    },
    "bag-check": {
        "hooks": ("What's in my bag?", "Never leaving home without it.", "Bag essentials."),
    },
    "before-after": {
        "cta": "See the difference.",
        "hooks": ("Before.", "Watch the change.", "Same light, no filter."),
    },
    "problem-to-app": {
        "hook": "Tired of this?",
        "hooks": ("Tired of this?", "I used to hate this.", "Stop doing it by hand."),
    },
    "customer-letter": {
        "hook": "A customer wrote to us.",
        "body": "What they said about {product}.",
        "cta": "Join them.",
        "hooks": ("A customer wrote to us.",),
    },
    "street-carry": {"body": "It goes everywhere I go."},
    "desk-break": {
        "body": "{product} gets me through.",
        "hooks": ("3 p.m. slump?", "Study break.", "How I get through the afternoon."),
    },
    "reaction-reveal": {
        "hooks": ("Wait, what?", "I did not expect this.", "Okay, this is good."),
    },
    "reaction-demo": {
        "hook": "I was about to give up.",
        "body": "Then {product} did it for me.",
        "hooks": ("I was about to give up.", "This app just saved me.", "Why did nobody tell me?"),
    },
    "value-stack": {"body": "Made better, built to last, easy to love."},
    "offer-drop": {
        "hook": "This week only.",
        "hooks": ("This week only.", "Last chance.", "Don't scroll past this."),
    },
    "swatch-test": {"hooks": ("Swatch check.", "No filter.", "On real skin.")},
    "expert-take": {
        "hook": "As a professional, here's what I use.",
        "hooks": ("As a professional, here's what I use.", "What I'd tell a client.", "Expert tip."),
    },
    "investigation": {
        "body": "I looked into {product}.",
        "cta": "See what I found.",
        "hooks": ("Is it actually worth it?", "I had to find out.", "Let's investigate."),
    },
}


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    seed = {spec["key"]: spec["metadata"] for spec in TEMPLATES}
    for row in option_model.objects.filter(kind="ad_template", key__in=list(seed)):
        new = seed[row.key]
        meta = dict(row.metadata or {})
        prompt = meta.get("keyframe_prompt")
        if isinstance(prompt, str) and prompt.endswith(OLD_FINISH):
            finish = CAPTURES[new["capture"]]["finish"]
            meta["keyframe_prompt"] = prompt.removesuffix(OLD_FINISH) + finish
        meta.setdefault("capture", new["capture"])
        if meta.get("negative") == OLD_NEGATIVE:
            meta["negative"] = new["negative"]
        for field, old in OLD_WORDS.get(row.key, {}).items():
            if field == "hooks":
                if meta.get("hooks") == list(old):  # type: ignore[call-overload]
                    meta["hooks"] = list(new["hooks"])
                continue
            script = dict(meta.get("script") or {})
            if script.get(field) == old:
                script[field] = new["script"][field]
                meta["script"] = script
        if meta != row.metadata:
            row.metadata = meta
            row.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0036_videorender_seed")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
