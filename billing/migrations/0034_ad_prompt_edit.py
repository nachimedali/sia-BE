"""`ad_prompt_edit` on the packages that carry it, where the row lacks the key.

Same rule as `0024`: a migration that has run does not run again, and an
operator's value is never overwritten. Off under test settings, like every seed.
"""

from django.db import migrations

from billing.management.commands.seed_plans import PACKAGES
from common.seeding import seeding_enabled


def forwards(apps, schema_editor):
    if not seeding_enabled():
        return
    plan_model = apps.get_model("billing", "Plan")
    for spec in PACKAGES:
        plan = plan_model.objects.filter(code=spec["code"]).first()
        if plan is None or "ad_prompt_edit" in plan.features:
            continue
        plan.features = {**plan.features, "ad_prompt_edit": spec["features"]["ad_prompt_edit"]}
        plan.save(update_fields=["features", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0033_invoice_reason_sha256")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
