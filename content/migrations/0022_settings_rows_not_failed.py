"""Settings rows that publishing wrongly marked FAILED go back to PENDING.

A platform's settings row (`social_account` empty, no `idempotency_key`)
holds the editor's content type and options; it is not a delivery target.
Before the fix, publishing tried it as one and failed it with "no connected
account", which showed a published post as failed. Only those rows move: a
delivery target whose account was deleted keeps its key and its failure.
"""

from django.db import migrations

MESSAGE = "This target has no connected account to publish through."


def reset(apps, schema_editor):  # type: ignore[no-untyped-def]
    PostTarget = apps.get_model("content", "PostTarget")
    PostTarget.objects.filter(
        social_account__isnull=True,
        idempotency_key__isnull=True,
        state="FAILED",
        error_detail__message=MESSAGE,
    ).update(state="PENDING", error_detail={}, attempt_count=0)


class Migration(migrations.Migration):
    dependencies = [("content", "0021_s18_template_rules")]

    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
