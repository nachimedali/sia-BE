"""Competitor tracking is retired from every package (2026-10-31).

The column stays, so the dormant `trends.services.competitors` still reads a
cap; every plan's cap becomes 0, so nothing can be tracked.
"""

from django.db import migrations


def zero_caps(apps, schema_editor):  # type: ignore[no-untyped-def]
    apps.get_model("billing", "Plan").objects.update(max_tracked_competitors=0)


class Migration(migrations.Migration):
    dependencies = [("billing", "0028_s18_credit_alert_levels")]

    operations = [migrations.RunPython(zero_caps, migrations.RunPython.noop)]
