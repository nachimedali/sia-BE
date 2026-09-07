"""The ledger re-scope half of the organization migration (P0-53, P0-61, P0-62).

The sequence was expand → backfill → ledger re-scope → dual-write → cut reads →
contract, and the order was not decorative: **reconciliation is extended before
the backfill runs**, because a backfill that moves rows into a dimension
nothing checks is a backfill whose mistakes are invisible until someone is
billed wrongly.

The workspace→organization backfill itself lives in `workspaces.0010` now that
the contract step has dropped the columns it read, and is exercised against the
real migration in `tests/test_phase0_gates.py` (P0-G1). What stays here is the
ledger re-scope — the sanctioned append-only exception — which the contract did
not touch.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection, transaction

from billing.models import CreditLedger
from billing.services import ledger, reconciliation
from common.models import MigrationNote

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# The ledger re-scope — P0-53
# -----------------------------------------------------------------------------
def test_the_ledger_backfill_recorded_why_it_wrote_to_an_append_only_table() -> None:
    """Part 7 rule 4 has one sanctioned exception and BUILD-PLAN requires it be
    recorded — so a reader who finds an UPDATE against a ledger table can tell
    a sanctioned re-scope from a bug."""
    notes = MigrationNote.objects.filter(migration="billing.0013_ledger_organization_backfill")

    assert {note.table for note in notes} == {"billing_creditledger", "billing_videoledger"}
    assert all("append-only" in note.reason.lower() for note in notes)


def _rescope_credit_ledger() -> int:
    """Runs the migration's fill statement, trigger dance and all.

    The dance is the point: `0003_ledger_append_only_trigger` blocks UPDATE on
    these tables in Postgres, which includes the migration that re-scopes them.
    A test that reached around the trigger would prove nothing about the
    migration that has to live with it.
    """
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS credit_ledger_append_only ON billing_creditledger")
        cursor.execute(
            "UPDATE billing_creditledger AS l SET organization_id = w.organization_id "
            "FROM workspaces_workspace AS w WHERE l.workspace_id = w.id "
            "AND w.organization_id IS NOT NULL "
            "AND l.organization_id IS DISTINCT FROM w.organization_id"
        )
        touched = cursor.rowcount
        cursor.execute(
            "CREATE TRIGGER credit_ledger_append_only BEFORE UPDATE OR DELETE ON "
            "billing_creditledger FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only()"
        )
    return int(touched)


def test_the_trigger_still_refuses_an_ordinary_update(workspace: Any) -> None:
    """The guard the re-scope has to step around is real, and stays real. If
    this ever passes silently, the migration left the trigger off."""
    from django.db.utils import ProgrammingError

    entry = ledger.grant_credits(workspace, 10, note="grant")[0]

    with pytest.raises(ProgrammingError), transaction.atomic():
        CreditLedger.objects.filter(pk=entry.pk).update(note="rewritten")


def test_re_running_the_ledger_fill_is_idempotent(workspace: Any, organization: Any) -> None:
    """The statement is a conditional UPDATE, so a second run touches nothing."""
    ledger.grant_credits(workspace, 10, note="grant")

    assert _rescope_credit_ledger() >= 1
    assert _rescope_credit_ledger() == 0


def test_reconciliation_notices_a_row_in_the_wrong_company(
    workspace: Any, organization: Any, other_user: Any
) -> None:
    """The finding that matters: a row attributed to the wrong company pools
    quota across a tenant boundary."""
    from workspaces.services.provisioning import provision_workspace

    entry = ledger.grant_credits(workspace, 10, note="grant")[0]
    stranger = provision_workspace(other_user, name="Another Company")
    assert stranger.organization is not None
    _misattribute(entry.pk, stranger.organization.pk)

    findings = reconciliation.reconcile_ledger_scope()

    assert [row["row"] for row in findings] == [entry.pk]


def test_reconciliation_is_clean_when_scopes_agree(workspace: Any, organization: Any) -> None:
    ledger.grant_credits(workspace, 10, note="grant")
    _rescope_credit_ledger()

    assert reconciliation.reconcile_ledger_scope() == []


def _misattribute(row_id: int, organization_id: int) -> None:
    """Puts a ledger row in the wrong company, around the trigger, so
    reconciliation has something to find. Only a test does this — in
    production the only writer is the migration above."""
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS credit_ledger_append_only ON billing_creditledger")
        cursor.execute(
            "UPDATE billing_creditledger SET organization_id = %s WHERE id = %s",
            [organization_id, row_id],
        )
        cursor.execute(
            "CREATE TRIGGER credit_ledger_append_only BEFORE UPDATE OR DELETE ON "
            "billing_creditledger FOR EACH ROW EXECUTE FUNCTION billing_ledger_append_only()"
        )


def test_the_org_sweep_reports_and_never_repairs(workspace: Any, organization: Any) -> None:
    """Part 7 rule 7, on the dimension the sweep still watches.

    Plan parity left the sweep with the contract step — one column cannot
    disagree with itself — so what it reports on is a ledger row filed under
    the wrong company, and it must leave that row exactly where it found it.
    The misfiling is the only evidence of the write path that caused it.
    """
    from django.contrib.auth import get_user_model

    from workspaces.services.provisioning import provision_workspace

    entry = ledger.grant_credits(workspace, 10, note="grant")[0]
    stranger = provision_workspace(
        get_user_model().objects.create_user(email="stranger@example.com", password="x"),
        name="Another Company",
    )
    _misattribute(entry.pk, stranger.organization.pk)

    assert reconciliation.reconcile_organizations() >= 1

    entry.refresh_from_db()
    assert entry.organization_id == stranger.organization.pk, (
        "the sweep repaired a row it should only have reported"
    )
