"""S19-01 and S19-08: video model profiles, and the Pro routes for Growth and Scale.

Profiles: `get_or_create` on the slug (`ai/video_profile_seed.py`), so a
profile edited in admin is never overwritten.

Pro routes (`ai/route_seed.PRO_ROUTES`): Growth and Scale's `video_start` and
`video_end` on Gemini Omni 1.2 Flash at 1080p, falling back to Kling 3.0 pro,
each row flagged `pro_rendering_s19` — resolved only for an organization with
the flag on, so with it off every package resolves exactly as before.
`get_or_create` on `(task, plan, flag)`.

Off under test settings, like every seed.
"""

from django.db import migrations

from ai.route_seed import FIELDS, PRO_ROUTES
from ai.video_profile_seed import FIELDS as PROFILE_FIELDS
from ai.video_profile_seed import PROFILES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    profile_model = apps.get_model("ai", "VideoModelProfile")
    for spec in PROFILES:
        profile_model.objects.get_or_create(
            slug=spec["slug"], defaults={key: spec[key] for key in PROFILE_FIELDS}
        )
    route_model = apps.get_model("ai", "AIModelRoute")
    plan_model = apps.get_model("billing", "Plan")
    for spec in PRO_ROUTES:
        plan = plan_model.objects.filter(code=spec["plan"]).first()
        if plan is None:
            continue
        route_model.objects.get_or_create(
            task=spec["task"],
            plan=plan,
            flag=spec["flag"],
            defaults={key: spec[key] for key in FIELDS},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0049_route_flag_and_video_profiles"),
        ("billing", "0022_packages_2026_10"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
