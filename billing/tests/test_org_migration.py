"""The organization migration sequence (P0-52, P0-53, P0-61, P0-62).

The sequence is expand → backfill → ledger re-scope → dual-write → cut reads →
contract, and the order is not decorative. Two steps here are what the rest
depends on:

* **reconciliation is extended before the backfill runs.** A backfill that
  moves rows into a dimension nothing checks is a backfill whose mistakes are
  invisible until someone is billed wrongly;
* **the backfill is idempotent, batched and resumable.** A migration that must
  complete in one shot is a migration that cannot be interrupted, and this one
  runs over every workspace in the deployment.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection, transaction

from billing.models import CreditLedger
from billing.services import ledger, plans, reconciliation
from common.models import MigrationNote
from workspaces.models import Membership, Organization, Role, Workspace

pytestmark = pytest.mark.django_db


def _orphan(user: Any, plan: Any, name: str = "Legacy Brand") -> Workspace:
    """A workspace as it looked before this phase: no organization, and a
    membership with no permission set. Constructed directly rather than through
    `provision_workspace`, which now makes an organization — the whole point is
    to have one that does not."""
    workspace = Workspace.objects.create(
        name=name, slug=Workspace.unique_slug(name), owner=user, plan=plan
    )
    Membership.objects.create(user=user, workspace=workspace, role=Role.OWNER, permissions=[])
    return workspace


def _run(**kwargs: Any) -> str:
    out = StringIO()
    call_command("backfill_organizations", stdout=out, **kwargs)
    return out.getvalue()


# -----------------------------------------------------------------------------
# The backfill — P0-52
# -----------------------------------------------------------------------------
def test_an_orphan_workspace_gets_an_organization(user: Any, plans_by_code: Any) -> None:
    """One org per existing workspace, named after it. That mapping is the only
    one that can be right without asking anybody: before this migration a
    workspace *was* the paying entity."""
    orphan = _orphan(user, plans_by_code["pro"])

    _run()

    orphan.refresh_from_db()
    assert orphan.organization is not None
    assert orphan.organization.owner == user
    assert orphan.organization.plan == plans_by_code["pro"]


def test_the_backfill_is_idempotent(user: Any, plans_by_code: Any) -> None:
    """A half-finished run can simply be started again."""
    _orphan(user, plans_by_code["pro"])

    _run()
    before = Organization.objects.count()
    _run()

    assert Organization.objects.count() == before


def test_a_workspace_that_already_has_one_is_left_alone(workspace: Any, organization: Any) -> None:
    _run()

    workspace.refresh_from_db()
    assert workspace.organization == organization


def test_it_is_resumable_from_a_cursor(user: Any, plans_by_code: Any) -> None:
    """The cursor is the last workspace id processed, so an operator who has to
    stop picks up where it stopped rather than re-reading what is done."""
    first = _orphan(user, plans_by_code["pro"], name="First")
    second = _orphan(user, plans_by_code["pro"], name="Second")

    _run(after=first.pk)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.organization is None
    assert second.organization is not None


def test_a_dry_run_writes_nothing(user: Any, plans_by_code: Any) -> None:
    _orphan(user, plans_by_code["pro"])

    output = _run(dry_run=True)

    assert Organization.objects.filter(name="Legacy Brand").count() == 0
    assert "would create" in output


def test_existing_members_join_the_company_too(
    user: Any, other_user: Any, plans_by_code: Any
) -> None:
    """Omitting them would leave collaborators visible in a brand and absent
    from the roster and quota of the company above it."""
    orphan = _orphan(user, plans_by_code["pro"])
    Membership.objects.create(user=other_user, workspace=orphan, role=Role.EDITOR)

    _run()

    orphan.refresh_from_db()
    company = orphan.organization
    assert company is not None
    assert company.memberships.count() == 2
    # OWNER is not assignable on a membership (P0-12), so the owner joins as
    # ADMIN — the same permission set, recorded in the one place that can hold it.
    assert company.memberships.get(user=user).role == Role.ADMIN


def test_permissions_are_derived_for_pre_expand_rows(user: Any, plans_by_code: Any) -> None:
    """Derivation is a pure function with an exhaustive 5x7 test behind it,
    which is the only reason this is safe to run unattended."""
    orphan = _orphan(user, plans_by_code["pro"])

    _run()

    membership = Membership.objects.get(user=user, workspace=orphan)
    assert "admin" in membership.permissions


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


def test_the_org_sweep_reports_and_never_repairs(
    workspace: Any, organization: Any, plans_by_code: Any
) -> None:
    """Part 7 rule 7. The drift is the only evidence that a write path bypassed
    `set_plan`, so repairing it would destroy the evidence."""
    workspace.plan = plans_by_code["advanced"]
    workspace.save(update_fields=["plan"])

    assert reconciliation.reconcile_organizations() >= 1

    workspace.refresh_from_db()
    organization.refresh_from_db()
    assert workspace.plan != organization.plan
    assert plans.parity_drift() != []
