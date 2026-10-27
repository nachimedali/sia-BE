"""Seeds S17 T3 and the T4 script writer.

* the multi-shot templates (`app-walkthrough`, `space-tour`);
* `AITask.AD_SCRIPT`, its `(TEXT, RECIPE)` price and a default route copied
  from the Studio text default row, so scripts run on the same model until an
  operator routes them elsewhere;
* talking formats at 3 units and interviews at 5 — only where a row still
  holds the 2 the first seed gave it.

`get_or_create` and add-only updates: an operator's edit is never overwritten.
Off under test settings, like every seed.
"""

from django.db import migrations, models

from ai.ad_template_seed import TEMPLATES
from common.seeding import seeding_enabled

KEYS = ("app-walkthrough", "space-tour")


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    option_model = apps.get_model("ai", "CreativeOption")
    for spec in TEMPLATES:
        meta = spec["metadata"]
        if spec["key"] in KEYS:
            defaults = {**spec, "is_active": True}
            key = defaults.pop("key")
            option_model.objects.get_or_create(kind="ad_template", key=key, defaults=defaults)
        elif meta["tier"] == "talking":
            for row in option_model.objects.filter(kind="ad_template", key=spec["key"]):
                if row.metadata.get("units") == {"clip-15": 2}:
                    row.metadata = {**row.metadata, "units": meta["units"]}
                    row.save(update_fields=["metadata"])

    apps.get_model("ai", "GenerationCost").objects.get_or_create(
        kind="TEXT", mode="RECIPE", provider="", model="", defaults={"credits": 1}
    )
    route_model = apps.get_model("ai", "AIModelRoute")
    text = route_model.objects.filter(task="studio_text", plan=None).first()
    if text is not None:
        route_model.objects.get_or_create(
            task="ad_script",
            plan=None,
            defaults={
                "provider": text.provider,
                "model": text.model,
                "fallback_model": text.fallback_model,
                "params": text.params,
            },
        )


class Migration(migrations.Migration):
    dependencies = [("ai", "0026_seed_ad_templates")]

    operations = [
        migrations.AlterField(
            model_name="aimodelroute",
            name="task",
            field=models.CharField(
                choices=[
                    ("studio_text", "Studio text (idea, product, rewrite, suggest, revision)"),
                    ("caption_from_image", "Caption from an image (vision)"),
                    ("image_copy", "Headline and caption for generated images"),
                    ("constraint_check", "Brand-constraint check on an image (vision)"),
                    ("rephrase", "Studio and motion brief rephrase"),
                    ("brand_import", "Brand and product import from a website"),
                    ("campaign_plan", "Campaign plan"),
                    ("digest", "Digest narration"),
                    ("tools", "Tool suite"),
                    ("sentiment", "Audience comment sentiment"),
                    ("inbox_coding", "Audience inbox coding"),
                    ("image_studio", "Studio images"),
                    ("image_autopilot", "Autopilot images"),
                    ("video_start", "Video from a first frame, and text-to-video"),
                    ("video_end", "Video landing on a last frame"),
                    ("avatar_portrait", "Avatar portraits and reference angles"),
                    ("ad_script", "Ad template scripts: hook, body and call to action"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
