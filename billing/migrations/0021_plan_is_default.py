"""`Plan.is_default`: which plan new organizations start on and lapsed
subscriptions return to (pricing.plan.md D-2).

It used to be the row coded `free`, by convention in three modules. The data
step keeps that meaning exactly on any database that has the row — always, not
only when seeding is on, because code reading `is_default` must find a floor on
every database it runs against. `0022` moves the mark to Spark where seeding is on.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    plan_model = apps.get_model("billing", "Plan")
    if not plan_model.objects.filter(is_default=True).exists():
        plan_model.objects.filter(code="free").update(is_default=True)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0020_plan_presentation"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="is_default",
            field=models.BooleanField(
                default=False,
                help_text="New organizations start on this plan; lapsed subscriptions return to it.",
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
