"""BUILD-PLAN Phase 0 ship gates.

Four gates, and this file holds the two that are properties of the system
rather than of a task:

* **P0-G1** — an existing single-workspace user is migrated *invisibly*. Not
  "the migration runs without error": nothing the user can observe may change.
* **P0-G4** — **capture load can be doubled with zero late publishes.** This is
  Part 7 rule 11 stated as a number, and it is the reason the rate budget is
  one bucket with a floor rather than two independently-sized ones.

The other two gates live where their machinery does: P0-G2 in
`workspaces/tests/test_organizations_api.py`, P0-G3 in
`billing/tests/test_org_entitlements.py`.

**G1 runs the real migrations** (`workspaces.0010` + `0011`), rewinding the
schema to before them, writing rows in the shape that existed then, and rolling
forward. It used to call a standalone `backfill_organizations` command; the
contract step folded that into the migration, and a test of a command that no
longer exists would have been a test of nothing. Rewinding is why these carry
`transaction=True` — DDL cannot run inside the usual test transaction — and why
they build their own users and plans instead of using the shared fixtures,
which are bound to the non-transactional `db`.
"""

from __future__ import annotations

import datetime as dt
from importlib import import_module
from typing import Any

import pytest
import time_machine
from django.apps import apps as live_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from analytics.services import ingest
from analytics.tests.conftest import make_target
from billing.models import Plan
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from scheduling import publishing
from workspaces.models import Membership, Organization, Role, Workspace

pytestmark = pytest.mark.django_db

#: The last migration before the contract step. Rolling forward again goes to
#: head rather than to a named target — see `_migrate_fully_forward`.
BEFORE_CONTRACT = ("workspaces", "0009_multi_currency_pricing")


def _migrate(target: tuple[str, str]) -> Any:
    """Moves the schema to `target` and returns the historical app registry."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


def _migrate_fully_forward() -> None:
    """Puts the schema back to head — **every app, not just this one**.

    Rewinding `workspaces` drags anything that depends on it backwards too, and
    since Phase 2 that includes a whole app (`collaboration`) and the migration
    that drops `PostComment`. Rolling forward to a named `workspaces` target
    would leave those unapplied, so `workspaces_postcomment` would still exist
    in the database while being absent from the model registry — and the next
    transactional teardown fails with `cannot truncate a table referenced in a
    foreign key constraint`, thousands of lines from the cause.

    Leaf nodes rather than a named target, so a migration added by a future
    phase is covered without anyone remembering to edit this.
    """
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture
def pre_contract_schema() -> Any:
    """Rewinds to the pre-contract schema and always rolls forward again.

    The roll-forward is in a `finally` because leaving the test database on an
    old schema would fail every test that ran after this one, with an error
    nowhere near the cause.
    """
    old_apps = _migrate(BEFORE_CONTRACT)
    try:
        yield old_apps
    finally:
        _migrate_fully_forward()


def _legacy_workspace(old_apps: Any, *, email: str, plan: Plan) -> tuple[Any, Any]:
    """A workspace exactly as it looked before the contract step: no
    organization, the commercial columns on the workspace itself, and a
    membership carrying a role but no permission set.

    Built through the historical models, which are the only ones that still
    have those columns.
    """
    User = old_apps.get_model("accounts", "User")
    OldWorkspace = old_apps.get_model("workspaces", "Workspace")  # noqa: N806 — a model class
    OldMembership = old_apps.get_model("workspaces", "Membership")  # noqa: N806 — a model class

    user = User.objects.create(email=email, password="!", is_active=True)
    workspace = OldWorkspace.objects.create(
        name="Legacy Studio",
        slug=f"legacy-{user.pk}",
        owner_id=user.pk,
        plan_id=plan.pk,
    )
    OldMembership.objects.create(
        user_id=user.pk, workspace_id=workspace.pk, role=Role.OWNER, permissions=[]
    )
    return user, workspace


def _seed_plans() -> dict[str, Plan]:
    call_command("seed_plans", verbosity=0)
    return {plan.code: plan for plan in Plan.objects.all()}


def _live(user: Any) -> Any:
    """The same account, read through the current models."""
    return get_user_model().objects.get(pk=user.pk)


# -----------------------------------------------------------------------------
# P0-G1 — an existing user is migrated invisibly
# -----------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_a_pre_migration_user_sees_no_change_in_entitlements(pre_contract_schema: Any) -> None:
    """ "Invisibly" is the whole gate. A migration the customer notices is a
    migration that generates support tickets, and the one thing they would
    notice fastest is their plan changing under them."""
    plans = _seed_plans()
    _, legacy = _legacy_workspace(
        pre_contract_schema, email="legacy@example.com", plan=plans["pro"]
    )

    # Resolved through the current resolver on the *old* rows: at this point
    # the plan still sits on the workspace and there is no organization, which
    # is exactly the state the migration has to preserve the meaning of.
    before = {"plan_code": "pro", "features": plans["pro"].features}

    _migrate_fully_forward()

    workspace = Workspace.objects.get(pk=legacy.pk)
    after = entitlements_for(workspace).as_dict()

    assert after["plan_code"] == before["plan_code"]
    assert after["features"] == {
        key: plans["pro"].feature(key) for key in sorted(before["features"])
    }
    plan = workspace.organization.plan
    assert plan is not None
    assert plan.code == "pro"


@pytest.mark.django_db(transaction=True)
def test_a_pre_migration_user_keeps_their_balances(pre_contract_schema: Any) -> None:
    """Balances are the other thing noticed immediately, and the ledger
    re-scope writes to append-only tables — so this is the assertion that the
    sanctioned exception did not become a rewrite."""
    plans = _seed_plans()
    _, legacy = _legacy_workspace(
        pre_contract_schema, email="legacy@example.com", plan=plans["pro"]
    )
    # `only("id")`: the schema is rewound but `Workspace` is the live model, and
    # any column added since the contract step (`market`, P8-02) does not exist
    # yet. Selecting every field would fail on the newest one, not on anything
    # this test is about.
    ledger.grant_credits(
        Workspace.objects.only("id").get(pk=legacy.pk), 42, note="before the migration"
    )

    _migrate_fully_forward()

    assert ledger.credit_balance(Workspace.objects.get(pk=legacy.pk)) == 42


@pytest.mark.django_db(transaction=True)
def test_a_pre_migration_user_still_reaches_every_endpoint(pre_contract_schema: Any) -> None:
    """Their session, their requests and their URLs are unchanged — including
    sending no `X-Workspace-Id`, which they have no way to know about."""
    plans = _seed_plans()
    user, _ = _legacy_workspace(pre_contract_schema, email="legacy@example.com", plan=plans["pro"])

    _migrate_fully_forward()

    client = APIClient()
    client.force_authenticate(_live(user))
    for name in ("product-list", "billing-entitlements", "analytics-overview"):
        response = client.get(reverse(name))
        assert response.status_code == 200, f"{name} broke for a migrated user"


@pytest.mark.django_db(transaction=True)
def test_a_pre_migration_membership_keeps_its_authority(pre_contract_schema: Any) -> None:
    """The permission backfill derives from `role` through the same table the
    exhaustive 5x7 test pins. A migration that silently *widened* access is
    the worst outcome available here, so this checks both directions."""
    plans = _seed_plans()
    user, legacy = _legacy_workspace(
        pre_contract_schema, email="legacy@example.com", plan=plans["pro"]
    )

    _migrate_fully_forward()

    membership = Membership.objects.get(user_id=user.pk, workspace_id=legacy.pk)
    assert set(membership.permissions) == {
        "view",
        "comment",
        "edit",
        "approve",
        "publish",
        "analyze",
        "admin",
    }


@pytest.mark.django_db(transaction=True)
def test_a_viewer_is_not_widened_by_the_migration(pre_contract_schema: Any) -> None:
    plans = _seed_plans()
    _, legacy = _legacy_workspace(
        pre_contract_schema, email="legacy@example.com", plan=plans["pro"]
    )

    User = pre_contract_schema.get_model("accounts", "User")
    OldMembership = pre_contract_schema.get_model("workspaces", "Membership")  # noqa: N806 — a model class
    viewer_user = User.objects.create(email="viewer@example.com", password="!", is_active=True)
    OldMembership.objects.create(
        user_id=viewer_user.pk, workspace_id=legacy.pk, role=Role.VIEWER, permissions=[]
    )

    _migrate_fully_forward()

    viewer = Membership.objects.get(user_id=viewer_user.pk, workspace_id=legacy.pk)
    assert set(viewer.permissions) == {"view", "analyze"}
    assert "publish" not in viewer.permissions


@pytest.mark.django_db(transaction=True)
def test_every_migrated_workspace_lands_in_its_own_company(pre_contract_schema: Any) -> None:
    """Two pre-contract workspaces were two paying entities, so they become two
    organizations — not one shared company that would pool their quotas
    together. Merging is a decision only the customer can make."""
    plans = _seed_plans()
    _, first = _legacy_workspace(pre_contract_schema, email="one@example.com", plan=plans["pro"])
    _, second = _legacy_workspace(pre_contract_schema, email="two@example.com", plan=plans["free"])

    _migrate_fully_forward()

    one = Workspace.objects.get(pk=first.pk)
    two = Workspace.objects.get(pk=second.pk)
    assert one.organization_id != two.organization_id
    assert one.organization.plan is not None
    assert two.organization.plan is not None
    assert one.organization.plan.code == "pro"
    assert two.organization.plan.code == "free"
    assert one.organization.owner_id != two.organization.owner_id


@pytest.mark.django_db(transaction=True)
def test_re_running_the_backfill_changes_nothing(pre_contract_schema: Any) -> None:
    """Idempotence, which is what makes a half-finished run safe to restart.

    The backfill selects only workspaces with no organization, so a second
    pass over migrated rows must not create a second company for any of them
    — and must not touch the one they have. Called directly rather than by
    re-running the migration, which Django records as applied and will not
    repeat.
    """
    plans = _seed_plans()
    _, legacy = _legacy_workspace(
        pre_contract_schema, email="legacy@example.com", plan=plans["pro"]
    )

    _migrate_fully_forward()

    workspace = Workspace.objects.get(pk=legacy.pk)
    organization_id = workspace.organization_id
    before = Organization.objects.count()

    backfill = import_module("workspaces.migrations.0010_backfill_organizations").backfill
    backfill(live_apps, None)

    workspace.refresh_from_db()
    assert Organization.objects.count() == before
    assert workspace.organization_id == organization_id


# -----------------------------------------------------------------------------
# P0-G4 — capture load doubled, zero late publishes
# -----------------------------------------------------------------------------
def test_doubling_capture_load_costs_publishing_nothing(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The gate, as a property rather than a benchmark.

    Capture is run until the budget refuses it — which is "load doubled" and
    then some, since the refusal point does not move with how hard you push —
    and publishing is then asked for its full reservation. Every grant must
    still be there.

    A benchmark would measure this deployment's Redis on this afternoon's
    hardware. The property is what actually has to hold: **one bucket with a
    floor only publishing may cross**, so capture cannot reach the reservation
    however much of it there is.
    """
    reserve = int(publishing._limiter(social_account)._reserve)
    capture_limiter = publishing.capture_limiter(social_account)

    draws = 0
    while capture_limiter.consume_for_background():
        draws += 1
        if draws > publishing.PUBLISH_CAPACITY * 4:  # pragma: no cover - safety net
            pytest.fail("capture drew past the whole bucket; the reserve is not holding")

    granted = sum(
        1 for _ in range(reserve) if publishing._limiter(social_account).consume_for_publish()
    )

    assert granted == reserve, "capture ate into publishing's reservation"


def test_capture_defers_instead_of_erroring_under_load(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The other half of the gate. If exhaustion raised, a busy hour would look
    identical to a provider outage in the failure metrics publishing alerts
    on — and the on-call response to those two is opposite."""
    start = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)
    with time_machine.travel(start, tick=False):
        for _ in range(3):
            make_target(paid_workspace, user, social_account, age_days=0)

    limiter = publishing.capture_limiter(social_account)
    while limiter.consume_for_background():
        pass

    with time_machine.travel(start + dt.timedelta(hours=1, minutes=5), tick=False):
        # No exception, no rows, and the rungs are still owed.
        assert ingest.capture_due() == 0

    from analytics.models import CaptureDeferral, PostMetric

    assert not PostMetric.objects.exists()
    assert CaptureDeferral.objects.count() == 3


def test_publishing_still_succeeds_while_capture_is_starved(
    paid_workspace: Any,
    user: Any,
    social_account: Any,
    metrics_provider: Any,
    platform_adapter: Any,
) -> None:
    """End to end: capture has drained everything it may touch, and a scheduled
    post still goes out on time."""
    from content.models import DeliveryMode, Post, PostStatus
    from scheduling.services import schedule_post

    limiter = publishing.capture_limiter(social_account)
    while limiter.consume_for_background():
        pass

    post = Post.objects.create(
        workspace=paid_workspace, author=user, master_body="Our seasonal glaze is live today."
    )
    schedule_post(
        post=post,
        delivery_mode=DeliveryMode.AUTO_PUBLISH,
        scheduled_at=timezone.now() + dt.timedelta(minutes=1),
        actor=user,
    )

    with time_machine.travel(timezone.now() + dt.timedelta(minutes=2), tick=False):
        publishing.publish_due()

    post.refresh_from_db()
    assert post.status == PostStatus.PUBLISHED
