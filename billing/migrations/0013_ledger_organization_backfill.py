"""Fills `organization` on both append-only ledgers (P0-53).

**This migration writes to append-only tables, and that is sanctioned.**
Part 7 rule 4 says corrections are compensating rows — but these rows are not
being corrected. They are being told which company they always belonged to, and
no compensating row can express that: a ledger's balance is the sum of its
deltas, so an "adjusting" row would change the balance rather than the scope.

BUILD-PLAN names this as the one exception and requires it be *recorded*, so a
future reader who finds an UPDATE against `billing_creditledger` can tell a
sanctioned re-scope from a bug. The `MigrationNote` row is that record.

Raw SQL rather than the ORM, because the ORM's save path is where the readable
half of the guard lives and going through it would simply be refused.

**The trigger has to come off, and that is the whole reason this file is so
loud.** `0003_ledger_append_only_trigger` enforces I4 in Postgres precisely so
that `QuerySet.update()`, a management shell and raw SQL cannot quietly rewrite
history — which includes this migration. So the trigger is dropped for exactly
these two statements and recreated immediately afterwards, inside the same
transaction: if anything here fails, the rollback restores the trigger along
with everything else, and there is no window in which the table is unprotected
and reachable.

One statement per table, no cursor: the join is on an indexed FK and this runs
once. Reversible by nulling the column back out — the forward direction loses
nothing, so the reverse loses nothing either.
"""

from django.db import migrations

#: Lifted verbatim from `0003_ledger_append_only_trigger`. Duplicated rather
#: than imported: a migration must keep working against the schema as it was
#: when the migration ran, and importing from another migration couples this
#: one to edits made years later.
DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS credit_ledger_append_only ON billing_creditledger;
DROP TRIGGER IF EXISTS video_ledger_append_only ON billing_videoledger;
"""

RESTORE_TRIGGERS = """
CREATE TRIGGER credit_ledger_append_only
    BEFORE UPDATE OR DELETE ON billing_creditledger
    FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only();
CREATE TRIGGER video_ledger_append_only
    BEFORE UPDATE OR DELETE ON billing_videoledger
    FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only();
"""

FILL = """
UPDATE {table} AS ledger
   SET organization_id = workspace.organization_id
  FROM workspaces_workspace AS workspace
 WHERE ledger.workspace_id = workspace.id
   AND workspace.organization_id IS NOT NULL
   AND ledger.organization_id IS DISTINCT FROM workspace.organization_id
"""

UNFILL = "UPDATE {table} SET organization_id = NULL"

TABLES = ("billing_creditledger", "billing_videoledger")


def fill(apps, schema_editor):
    note_model = apps.get_model("common", "MigrationNote")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_TRIGGERS)
        for table in TABLES:
            cursor.execute(FILL.format(table=table))
            note_model.objects.create(
                table=table,
                migration="billing.0013_ledger_organization_backfill",
                reason=(
                    "Append-only re-scope to Organization (BUILD-PLAN P0-53). Rows were not "
                    "corrected; they were attributed to the company that always owned them. "
                    "No compensating row can express a scope change, because a ledger balance "
                    "is the sum of its deltas."
                ),
                rows_touched=cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0,
            )
        # Back on before this transaction commits. A failure above rolls the
        # drop back with everything else, so the table is never left unguarded.
        cursor.execute(RESTORE_TRIGGERS)


def unfill(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_TRIGGERS)
        for table in TABLES:
            cursor.execute(UNFILL.format(table=table))
        cursor.execute(RESTORE_TRIGGERS)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0012_ledger_organization_scope"),
        ("common", "0002_migration_note"),
        ("workspaces", "0008_workspace_status"),
    ]

    operations = [migrations.RunPython(fill, unfill)]
