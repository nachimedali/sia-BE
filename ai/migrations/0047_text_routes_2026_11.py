"""Text routes off the NVIDIA models that stopped answering (`ai/route_seed.py`).

On 2026-11-12 the hosted API answered 404 for `muse-glimmer-30b` and 410 for
`llama-3.2-11b-vision`, and the deepseek and gemma fallbacks timed out, so every
text task failed. A route still on a pair `0021` seeded moves to the new seed,
`thinking` off; a route an operator edited is left alone. Each move is a route
change, as `seed_model_routes --refresh` writes. Off under test settings, like
every seed.
"""

from django.db import migrations

from ai.route_seed import FIELDS, ROUTES
from common.seeding import seeding_enabled

# The (model, fallback) pairs `0021` seeded, frozen here: the seed no longer names them.
DEAD = {
    ("meta/muse-glimmer-30b", "deepseek-ai/deepseek-v4.1-flash"),
    ("meta/muse-glimmer-30b", "meta/llama-3.2-11b-vision-instruct"),
    ("nvidia/nemotron-3.5-lightning-30b-a3b", "google/gemma-4-31b-it"),
}
SNAPSHOT = ("provider", "model", "fallback_model", "params", "is_active")


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    route_model = apps.get_model("ai", "AIModelRoute")
    change_model = apps.get_model("ai", "AIModelRouteChange")
    for spec in ROUTES:
        if spec["plan"]:
            continue
        route = route_model.objects.filter(task=spec["task"], plan__isnull=True).first()
        if route is None or (route.model, route.fallback_model) not in DEAD:
            continue
        before = {key: getattr(route, key) for key in SNAPSHOT}
        for key in FIELDS:
            setattr(route, key, spec[key])
        route.save()
        change_model.objects.create(
            route=route, before=before, after={key: getattr(route, key) for key in SNAPSHOT}
        )


class Migration(migrations.Migration):
    dependencies = [("ai", "0046_template_previews")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
