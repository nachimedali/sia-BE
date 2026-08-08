"""Enforces I4 at the table, not just in Python.

The model guard in `LedgerEntry.save()/delete()` catches the ordinary mistake and
gives a readable error. It does not catch `QuerySet.update()`, `bulk_update`, a
cascade, a management shell, or raw SQL — and the ledgers are the record a
billing dispute is settled from, so "we thought nothing wrote to it that way" is
not a good enough guarantee.

design.md I4 offers "no UPDATE/DELETE permission, or a DB trigger". A trigger is
chosen over grants because it travels with the migration: a new environment,
a restored backup and a developer's laptop all get the same protection without
anyone remembering to run a GRANT.

`TRUNCATE` is deliberately not covered — Django's test teardown uses it, and it
is not a way to quietly alter one row's history.
"""

from __future__ import annotations

from django.db import migrations

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION billing_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'ledger rows are append-only (I4): write a compensating row instead of %  on %',
        TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

DROP_FUNCTION_SQL = "DROP FUNCTION IF EXISTS billing_ledger_append_only();"


def _trigger(table: str, name: str) -> str:
    return f"""
    CREATE TRIGGER {name}
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only();
    """


def _drop_trigger(table: str, name: str) -> str:
    return f"DROP TRIGGER IF EXISTS {name} ON {table};"


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_stripeevent_creditledger_subscription_videoledger"),
    ]

    # Operations unwind last-first, so the function is created by the first
    # operation and dropped by its reverse — after both triggers that depend on
    # it have already gone.
    operations = [
        migrations.RunSQL(sql=FUNCTION_SQL, reverse_sql=DROP_FUNCTION_SQL),
        migrations.RunSQL(
            sql=_trigger("billing_creditledger", "credit_ledger_append_only"),
            reverse_sql=_drop_trigger("billing_creditledger", "credit_ledger_append_only"),
        ),
        migrations.RunSQL(
            sql=_trigger("billing_videoledger", "video_ledger_append_only"),
            reverse_sql=_drop_trigger("billing_videoledger", "video_ledger_append_only"),
        ),
    ]
