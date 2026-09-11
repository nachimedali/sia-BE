"""Drop `PostComment`, now that `collaboration.0002` has carried its rows over.

**Collapsed under the pre-launch exemption** (BUILD-PLAN Part 3, recorded
2026-09-07): expand/dual-write/cut-reads/contract protects live rows while old
and new code run side by side, and `app-BE/` has never been deployed. Holding a
shadow table open to launch would not de-risk anything — it would carry the
duplicate to production, which is strictly worse. The dependency on
`collaboration.0002_threads_from_post_comments` is what makes the collapse safe:
the rows are read before the table is gone, in one `migrate`.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0011_contract_workspace_commercial"),
        ("collaboration", "0002_threads_from_post_comments"),
    ]

    operations = [
        migrations.DeleteModel(name="PostComment"),
    ]
