"""How a plan presents itself (P4, P8), and one L-2 correction.

Schema: `Plan.is_recommended` (the pricing page's badge, chosen in admin rather
than by position) and `Plan.support_response` (the help page's reply time).

Data, in two parts:

- **Always:** no plan sells auto-approval. L-2 refuses it on every tier, and
  Advanced was seeded with `autopilot_auto_approve: true` and a tagline saying
  so. This is a correction, not a seed, so it runs even where seeding is off.
- **When seeding is on, never overwriting an edit:** Pro becomes the
  recommended plan if no plan is yet; each plan gets its reply time if it has
  none; Free leaves the public list (L-4: the quota trial replaced it), only if
  it is still public as seeded.
"""

from django.db import migrations, models

from common.seeding import seeding_enabled

OLD_ADVANCED_TAGLINE = "Approvals, API access and auto-approve autopilot."
NEW_ADVANCED_TAGLINE = "Approval chains, API access and the most video."

SUPPORT = {
    "trial": "2 working days",
    "free": "2 working days",
    "pro": "1 working day",
    "advanced": "Same working day",
}


def forwards(apps, schema_editor):
    plan_model = apps.get_model("billing", "Plan")

    for plan in plan_model.objects.all():
        changed = []
        if plan.features.get("autopilot_auto_approve"):
            plan.features = {**plan.features, "autopilot_auto_approve": False}
            changed.append("features")
        if plan.tagline == OLD_ADVANCED_TAGLINE:
            plan.tagline = NEW_ADVANCED_TAGLINE
            changed.append("tagline")
        if changed:
            plan_model.objects.filter(pk=plan.pk).update(**{f: getattr(plan, f) for f in changed})

    if not seeding_enabled():
        return

    for code, words in SUPPORT.items():
        plan_model.objects.filter(code=code, support_response="").update(support_response=words)
    if not plan_model.objects.filter(is_recommended=True).exists():
        plan_model.objects.filter(code="pro").update(is_recommended=True)
    plan_model.objects.filter(code="free", is_public=True).update(is_public=False)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0019_reply_refunds"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="is_recommended",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="plan",
            name="support_response",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
