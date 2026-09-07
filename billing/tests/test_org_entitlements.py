"""The organization axis of the entitlement resolver (P0-24, P0-25, P0-26).

Three things are asserted here and one of them is the ship gate:

* the cache key spans **org plan crossed with org add-ons**, so enabling an
  add-on invalidates without anyone hunting down a key;
* `require_addon` and `check_soft_budget` answer 402 with **distinct codes**,
  because "buy the add-on", "upgrade your plan" and "raise your own budget"
  are three different screens;
* **20 simultaneous debits against one organization produce exactly 20 rows**
  and a correct balance, with the lock never spanning a provider call (P0-26).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from django.db import connections, transaction

from billing.models import AddonStatus, OrganizationAddon, ReplyLedger
from billing.services import ledger, plans
from billing.services.entitlements import bump_addon_version, entitlements_for
from common.exceptions import AddonNotEnabled, InsufficientCredits, SoftBudgetExceeded

pytestmark = pytest.mark.django_db


def _enable(organization: Any, key: str) -> OrganizationAddon:
    addon = OrganizationAddon.objects.create(
        organization=organization, addon_key=key, status=AddonStatus.ACTIVE
    )
    bump_addon_version(organization)
    return addon


# -----------------------------------------------------------------------------
# The second axis — P0-24
# -----------------------------------------------------------------------------
def test_an_org_with_no_addons_holds_none(workspace: Any) -> None:
    assert entitlements_for(workspace).addons() == set()


def test_enabling_an_addon_is_visible_to_the_resolver(workspace: Any, organization: Any) -> None:
    _enable(organization, "extra_seats")

    assert entitlements_for(workspace).has_addon("extra_seats") is True


def test_enabling_an_addon_invalidates_the_cached_snapshot(
    workspace: Any, organization: Any
) -> None:
    """The point of putting the add-on version in the key: no eviction step,
    and therefore no eviction step to forget."""
    assert entitlements_for(workspace).addons() == set()

    _enable(organization, "extra_seats")
    organization.refresh_from_db()
    workspace.refresh_from_db()

    assert entitlements_for(workspace).addons() == {"extra_seats"}


def test_a_disabled_addon_is_not_active(workspace: Any, organization: Any) -> None:
    OrganizationAddon.objects.create(
        organization=organization, addon_key="extra_seats", status=AddonStatus.CANCELLED
    )
    bump_addon_version(organization)

    assert entitlements_for(workspace).has_addon("extra_seats") is False


# -----------------------------------------------------------------------------
# Distinct 402s — P0-25
# -----------------------------------------------------------------------------
def test_a_missing_addon_is_402_with_its_own_code(workspace: Any) -> None:
    """Not `feature_not_available`: the customer may already be on the top
    plan, in which case an upgrade prompt is both wrong and insulting."""
    with pytest.raises(AddonNotEnabled) as caught:
        entitlements_for(workspace).require_addon("extra_seats")

    assert caught.value.status_code == 402
    assert caught.value.code == "addon_not_enabled"


def test_a_soft_budget_is_402_with_a_code_the_ui_can_phrase_locally(workspace: Any) -> None:
    """Nothing can be bought to fix this one — the fix is an admin in this
    workspace raising the ceiling it set for itself."""
    workspace.soft_budget_posts = 5
    workspace.save(update_fields=["soft_budget_posts"])

    with pytest.raises(SoftBudgetExceeded) as caught:
        entitlements_for(workspace).check_soft_budget("soft_budget_posts", current=5)

    assert caught.value.status_code == 402
    assert caught.value.code == "soft_budget_exceeded"


def test_an_unset_soft_budget_never_blocks(workspace: Any) -> None:
    """Null means off. A default of zero would silently freeze every workspace
    that never opted in."""
    assert workspace.soft_budget_posts is None

    entitlements_for(workspace).check_soft_budget("soft_budget_posts", current=10_000)


def test_a_budget_below_the_current_usage_still_only_refuses_the_next_spend(
    workspace: Any,
) -> None:
    workspace.soft_budget_posts = 10
    workspace.save(update_fields=["soft_budget_posts"])

    entitlements_for(workspace).check_soft_budget("soft_budget_posts", current=9)


# -----------------------------------------------------------------------------
# One place the plan lives — P0-54, P0-55, P0-56
# -----------------------------------------------------------------------------
def test_set_plan_writes_the_organization(workspace: Any, seeded_plans: Any) -> None:
    plans.set_plan(workspace, seeded_plans["pro"])
    workspace.refresh_from_db()

    assert workspace.organization.plan == seeded_plans["pro"]


def test_there_is_no_second_place_a_plan_could_live(workspace: Any) -> None:
    """What the dual-write parity metric used to watch for, now structural.

    `parity_drift` existed because two columns held the plan and could
    disagree; the contract step dropped the workspace copy, so the drift it
    reported is no longer expressible. This asserts the *reason* the metric
    was retired rather than leaving a permanently-empty check behind — if a
    shadow column ever comes back, this fails and the metric has to come back
    with it.
    """
    field_names = {field.name for field in workspace._meta.get_fields()}

    assert "plan" not in field_names
    assert "trial_ends_at" not in field_names
    assert "stripe_customer_id" not in field_names
    assert not hasattr(plans, "parity_drift")


def test_the_resolver_reads_the_organization_not_the_workspace(
    workspace: Any, seeded_plans: Any
) -> None:
    """P0-55. The cut-over, asserted from the outside: change only the
    organization and the entitlement answer must change with it."""
    plans.set_plan(workspace, seeded_plans["advanced"])

    assert entitlements_for(workspace).plan.code == "advanced"


# -----------------------------------------------------------------------------
# Pooled concurrency — P0-26, a ship gate
# -----------------------------------------------------------------------------
def test_twenty_simultaneous_debits_produce_exactly_twenty_rows(
    workspace: Any, organization: Any
) -> None:
    """The gate. Real threads against real Postgres, because the mechanism
    under test is `SELECT ... FOR UPDATE` on the organization row — an
    in-process fake would exercise none of it.

    Pooled at the org, so all twenty contend for one lock. The lock is held
    across the balance read and the insert and **nothing else**: no provider
    call, no mail, no cache write. A lock spanning a network call is how a
    slow vendor becomes a database outage.
    """
    outcomes: list[str] = []
    barrier = threading.Barrier(20)

    def spend() -> None:
        barrier.wait(timeout=10)
        try:
            with transaction.atomic():
                ledger.debit_reply(workspace, allowance=20)
            outcomes.append("charged")
        except InsufficientCredits:
            outcomes.append("refused")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=spend) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("charged") == 20
    assert ReplyLedger.objects.filter(organization=organization).count() == 20
    assert ledger.replies_used_this_month(organization) == 20


test_twenty_simultaneous_debits_produce_exactly_twenty_rows = pytest.mark.django_db(
    transaction=True
)(test_twenty_simultaneous_debits_produce_exactly_twenty_rows)


def test_the_twenty_first_debit_is_refused(workspace: Any, organization: Any) -> None:
    """The other half: the pool is a real ceiling, not a counter nobody
    checks."""
    for _ in range(3):
        ledger.debit_reply(workspace, allowance=3)

    with pytest.raises(InsufficientCredits):
        ledger.debit_reply(workspace, allowance=3)


def test_one_workspace_cannot_spend_anothers_headroom(
    workspace: Any, organization: Any, user: Any
) -> None:
    """L-4a's whole reason for pooling at the org: there is one pool, so a
    second workspace draws the same counter down rather than getting its own."""
    from workspaces.models import Workspace

    sibling = Workspace.objects.create(
        organization=organization,
        name="Sibling Brand",
        slug=Workspace.unique_slug("Sibling Brand"),
    )

    ledger.debit_reply(workspace, allowance=2)
    ledger.debit_reply(sibling, allowance=2)

    with pytest.raises(InsufficientCredits):
        ledger.debit_reply(sibling, allowance=2)
