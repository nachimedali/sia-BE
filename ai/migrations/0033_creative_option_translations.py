"""French and Italian wording for the seeded creative options (L-6).

Fills `translations` only where it is still empty, so wording an operator
already entered in admin is never overwritten.
"""

from __future__ import annotations

from django.db import migrations, models

from ai.creative_i18n import translations_for


def fill(apps, schema_editor) -> None:  # noqa: ANN001
    CreativeOption = apps.get_model("ai", "CreativeOption")
    for row in CreativeOption.objects.all():
        found = translations_for(row.kind, row.key)
        if found and not row.translations:
            row.translations = found
            row.save(update_fields=["translations"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0032_drop_made_in_tunisia")]

    operations = [
        migrations.AddField(
            model_name="creativeoption",
            name="translations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
