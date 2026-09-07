"""Schema half of the contract step (P0-56).

`0010` gave every workspace an organization; this makes that required and drops
the four columns that moved up to it. Separate migration, separate transaction
— see 0010's note on pending trigger events.

Reverse re-adds the columns empty. It restores the schema, not the split: which
workspace in a multi-brand company owned which value is not recoverable, and
guessing would be worse than an obviously blank column.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0010_backfill_organizations"),
        ("billing", "0016_subscription_overage_item"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="workspace",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="workspaces",
                to="workspaces.organization",
            ),
        ),
        migrations.RemoveField(model_name="workspace", name="plan"),
        migrations.RemoveField(model_name="workspace", name="owner"),
        migrations.RemoveField(model_name="workspace", name="trial_ends_at"),
        migrations.RemoveField(model_name="workspace", name="stripe_customer_id"),
    ]
