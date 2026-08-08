"""The ledgers (design.md §8.2, I4).

`balance_after` is a cache. The whole point of these tests is that it can never
disagree with `SUM(delta)`, and that two concurrent spenders cannot both take
the last credit.
"""

from __future__ import annotations

import contextlib
import random
import threading

import pytest
from django.db import connection, connections, transaction

from billing.models import (
    UNLIMITED,
    AppendOnlyError,
    CreditLedger,
    CreditReason,
    VideoLedger,
    VideoReason,
)
from billing.services import ledger
from common.exceptions import InsufficientCredits, InsufficientVideoUnits

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(plans, user):
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


# -----------------------------------------------------------------------------
# I4 — append-only, balance_after == SUM(delta)
# -----------------------------------------------------------------------------
def test_ledger_balance_matches_sum_of_deltas(workspace) -> None:
    """I4. A randomised sequence, because a hand-picked one only proves the
    cases someone thought of."""
    random.seed(20260804)
    ledger.grant_credits(workspace, 400, note="opening")

    for _ in range(60):
        action = random.choice(("debit", "refund", "adjust"))
        if action == "debit":
            cost = random.randint(1, 5)
            with contextlib.suppress(InsufficientCredits):
                ledger.debit_credits(workspace, cost, quota=400)
        elif action == "refund":
            debit = (
                CreditLedger.objects.filter(workspace=workspace, reason=CreditReason.GENERATION)
                .order_by("?")
                .first()
            )
            if debit is not None:
                ledger.refund_credits(debit)
        else:
            ledger.adjust_credits(workspace, random.randint(-3, 3), actor=None, note="drift test")

    assert ledger.find_drift(CreditLedger, workspace) == []

    running = 0
    for entry in CreditLedger.objects.filter(workspace=workspace).order_by("created_at", "id"):
        running += entry.delta
        assert entry.balance_after == running
    assert running == ledger.credit_balance(workspace)


def test_concurrent_debits_cannot_overspend_last_credit(workspace) -> None:
    """I4. Two threads, one credit. Exactly one may win.

    Uses real threads against real Postgres: the whole mechanism under test is
    `SELECT ... FOR UPDATE`, which an in-process fake or a mocked transaction
    would not exercise at all.
    """
    ledger.grant_credits(workspace, 1, note="one credit")

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def spend() -> None:
        barrier.wait(timeout=5)
        try:
            with transaction.atomic():
                ledger.debit_credits(workspace, 1, quota=1)
            outcomes.append("charged")
        except InsufficientCredits:
            outcomes.append("refused")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=spend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["charged", "refused"]
    assert ledger.credit_balance(workspace) == 0
    assert ledger.find_drift(CreditLedger, workspace) == []


# The lock is only observable across real connections, so this test needs the
# transactional database rather than the usual wrapped-in-a-rollback one.
test_concurrent_debits_cannot_overspend_last_credit = pytest.mark.django_db(transaction=True)(
    test_concurrent_debits_cannot_overspend_last_credit
)


def test_ledger_rows_cannot_be_updated_or_deleted_in_python(workspace) -> None:
    """I4, first guard: the readable error."""
    entry = ledger.grant_credits(workspace, 10)[0]

    entry.delta = 999
    with pytest.raises(AppendOnlyError):
        entry.save()
    with pytest.raises(AppendOnlyError):
        entry.delete()


def test_ledger_rows_cannot_be_updated_or_deleted_in_sql(workspace) -> None:
    """I4, second guard: the one that holds when someone reaches past the model.

    `QuerySet.update()` never calls `Model.save()`, so without the trigger this
    is exactly how a ledger quietly gets rewritten.
    """
    ledger.grant_credits(workspace, 10)

    with pytest.raises(Exception, match="append-only"), transaction.atomic():
        CreditLedger.objects.filter(workspace=workspace).update(delta=0)

    with pytest.raises(Exception, match="append-only"), transaction.atomic():
        CreditLedger.objects.filter(workspace=workspace).delete()

    with (
        pytest.raises(Exception, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("UPDATE billing_creditledger SET balance_after = 9999")


# -----------------------------------------------------------------------------
# Debit / refund
# -----------------------------------------------------------------------------
def test_refund_nets_to_zero_and_writes_compensating_row(workspace) -> None:
    ledger.grant_credits(workspace, 10)
    before = ledger.credit_balance(workspace)

    debit = ledger.debit_credits(workspace, 3, quota=150)
    assert ledger.credit_balance(workspace) == before - 3

    refund = ledger.refund_credits(debit)

    assert refund.reason == CreditReason.REFUND
    assert refund.reverses_id == debit.pk
    assert refund.delta == 3
    assert ledger.credit_balance(workspace) == before
    # The debit is still there: the history survives the correction.
    assert CreditLedger.objects.filter(pk=debit.pk).exists()


def test_refund_is_idempotent_so_a_retried_task_cannot_pay_twice(workspace) -> None:
    ledger.grant_credits(workspace, 10)
    debit = ledger.debit_credits(workspace, 3, quota=150)

    first = ledger.refund_credits(debit)
    second = ledger.refund_credits(debit)

    assert first.pk == second.pk
    assert CreditLedger.objects.filter(reverses=debit).count() == 1


def test_debit_refuses_when_the_balance_is_short(workspace) -> None:
    ledger.grant_credits(workspace, 2)

    with pytest.raises(InsufficientCredits) as excinfo:
        ledger.debit_credits(workspace, 3, quota=150)

    assert excinfo.value.payload == {"required": 3, "available": 2}
    assert excinfo.value.status_code == 402
    assert excinfo.value.upgrade["cta"] == "/app/billing"


def test_unlimited_plan_still_writes_zero_delta_rows(workspace) -> None:
    """§4.1 — usage telemetry must not go dark on the top tier."""
    ledger.grant_credits(workspace, 0)
    before = CreditLedger.objects.filter(workspace=workspace).count()

    entry = ledger.debit_credits(workspace, 3, quota=UNLIMITED)

    assert entry.delta == 0
    assert entry.reason == CreditReason.GENERATION
    assert CreditLedger.objects.filter(workspace=workspace).count() == before + 1
    assert ledger.credit_balance(workspace) == 0


def test_a_negative_debit_is_a_programming_error(workspace) -> None:
    with pytest.raises(ValueError):
        ledger.debit_credits(workspace, -5, quota=150)


def test_manual_adjustment_requires_a_note(workspace) -> None:
    with pytest.raises(ValueError):
        ledger.adjust_credits(workspace, 10, actor=None, note="")


# -----------------------------------------------------------------------------
# Grants
# -----------------------------------------------------------------------------
def test_credits_do_not_roll_over_on_grant(workspace) -> None:
    ledger.grant_credits(workspace, 150)
    ledger.debit_credits(workspace, 50, quota=150)
    assert ledger.credit_balance(workspace) == 100

    rows = ledger.grant_credits(workspace, 150, rollover=False)

    assert ledger.credit_balance(workspace) == 150
    # Two rows, not one net figure: "you were given 150 and 100 expired" is the
    # answer a billing question needs.
    assert [row.delta for row in rows] == [-100, 150]


def test_credits_roll_over_when_the_plan_says_so(workspace) -> None:
    ledger.grant_credits(workspace, 150)
    ledger.debit_credits(workspace, 50, quota=150)

    ledger.grant_credits(workspace, 150, rollover=True)

    assert ledger.credit_balance(workspace) == 250


def test_video_grant_resets_the_allowance_but_keeps_purchased_packs(workspace) -> None:
    """§4.3 — included videos reset monthly; prepaid packs are bought and stay."""
    ledger.grant_video_units(workspace, 4)
    ledger._append(VideoLedger, workspace, delta=10, reason=VideoReason.PURCHASE)
    assert ledger.video_balance(workspace) == 14

    ledger.grant_video_units(workspace, 4)

    assert ledger.video_balance(workspace) == 14  # 10 purchased + 4 fresh allowance


def test_video_debit_refuses_beyond_the_allowance(workspace) -> None:
    ledger.grant_video_units(workspace, 1)

    ledger.debit_video_units(workspace, 1, quota=4, unit_cost_cents=120)
    with pytest.raises(InsufficientVideoUnits):
        ledger.debit_video_units(workspace, 1, quota=4)


def test_video_refund_returns_the_unit(workspace) -> None:
    ledger.grant_video_units(workspace, 4)
    debit = ledger.debit_video_units(workspace, 1, quota=4, unit_cost_cents=120)

    ledger.refund_video_units(debit)

    assert ledger.video_balance(workspace) == 4
    assert ledger.find_drift(VideoLedger, workspace) == []


def test_reconciliation_reports_drift_rather_than_repairing_it(workspace) -> None:
    """Drift is the only symptom of a bug in the debit path. A task that fixed
    it silently would destroy the evidence.

    The drift is created by inserting straight into the table — which is the
    real failure mode, since the append-only trigger makes rewriting an existing
    row impossible but says nothing about what a bad INSERT may claim.
    """
    from billing.services.reconciliation import reconcile_workspace

    ledger.grant_credits(workspace, 100)

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO billing_creditledger "
            "(workspace_id, delta, balance_after, reason, note, created_at) "
            # clock_timestamp(), not NOW(): NOW() is the transaction's start
            # time, which would sort this row before the grant it follows.
            "VALUES (%s, %s, %s, %s, '', clock_timestamp())",
            [workspace.pk, -10, 777, CreditReason.GENERATION],
        )

    findings = reconcile_workspace(workspace)

    assert len(findings) == 1
    assert findings[0]["recorded"] == 777
    assert findings[0]["expected"] == 90
    assert CreditLedger.objects.filter(balance_after=777).exists(), (
        "reconciliation must report, never repair"
    )
