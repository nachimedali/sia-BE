"""BUILD-PLAN Phase 0 ship gates.

Four gates, and this file holds the two that are properties of the system
rather than of a task:

* **P0-G1** — an existing single-workspace user is migrated *invisibly*. Not
  "the backfill runs without error": nothing the user can observe may change.
* **P0-G4** — **capture load can be doubled with zero late publishes.** This is
  Part 7 rule 11 stated as a number, and it is the reason the rate budget is
  one bucket with a floor rather than two independently-sized ones.

The other two gates live where their machinery does: P0-G2 in
`workspaces/tests/test_organizations_api.py`, P0-G3 in
`billing/tests/test_org_entitlements.py`.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO
from typing import Any

import pytest
import time_machine
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from analytics.services import ingest
from analytics.tests.conftest import make_target
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from scheduling import publishing
from workspaces.models import Membership, Role, Workspace

pytestmark = pytest.mark.django_db


def _pre_migration_workspace(user: Any, plan: Any) -> Workspace:
    """A workspace exactly as it looked before this phase: no organization, and
    a membership carrying a role but no permission set.

    Constructed directly rather than through `provision_workspace`, which now
    builds the whole hierarchy — the point is to have one that does not.
    """
    workspace = Workspace.objects.create(
        name="Legacy Studio", slug=Workspace.unique_slug("Legacy Studio"), owner=user, plan=plan
    )
    Membership.objects.create(user=user, workspace=workspace, role=Role.OWNER, permissions=[])
    return workspace


# -----------------------------------------------------------------------------
# P0-G1 — an existing user is migrated invisibly
# -----------------------------------------------------------------------------
def test_a_pre_migration_user_sees_no_change_in_entitlements(
    user: Any, plans: dict[str, Any]
) -> None:
    """ "Invisibly" is the whole gate. A migration the customer notices is a
    migration that generates support tickets, and the one thing they would
    notice fastest is their plan changing under them."""
    workspace = _pre_migration_workspace(user, plans["pro"])
    before = entitlements_for(workspace).as_dict()

    call_command("backfill_organizations", stdout=StringIO())

    workspace.refresh_from_db()
    after = entitlements_for(workspace).as_dict()

    assert after["plan_code"] == before["plan_code"]
    assert after["features"] == before["features"]
    assert after["quotas"] == before["quotas"]


def test_a_pre_migration_user_keeps_their_balances(user: Any, plans: dict[str, Any]) -> None:
    """Balances are the other thing noticed immediately, and the ledger
    re-scope writes to append-only tables — so this is the assertion that the
    sanctioned exception did not become a rewrite."""
    workspace = _pre_migration_workspace(user, plans["pro"])
    ledger.grant_credits(workspace, 42, note="before the migration")

    call_command("backfill_organizations", stdout=StringIO())

    assert ledger.credit_balance(workspace) == 42


def test_a_pre_migration_user_still_reaches_every_endpoint(
    auth_client: Any, user: Any, plans: dict[str, Any]
) -> None:
    """Their session, their requests and their URLs are unchanged — including
    sending no `X-Workspace-Id`, which they have no way to know about."""
    workspace = _pre_migration_workspace(user, plans["pro"])
    Workspace.objects.filter(pk=workspace.pk).exclude(pk=workspace.pk)

    call_command("backfill_organizations", stdout=StringIO())

    for name in ("product-list", "billing-entitlements", "analytics-overview"):
        response = auth_client.get(reverse(name))
        assert response.status_code == 200, f"{name} broke for a migrated user"


def test_a_pre_migration_membership_keeps_its_authority(user: Any, plans: dict[str, Any]) -> None:
    """The permission backfill derives from `role` through the pure function
    the exhaustive 5x7 test pins. A migration that silently *widened* access is
    the worst outcome available here, so this checks both directions."""
    workspace = _pre_migration_workspace(user, plans["pro"])

    call_command("backfill_organizations", stdout=StringIO())

    membership = Membership.objects.get(user=user, workspace=workspace)
    assert set(membership.permissions) == {
        "view",
        "comment",
        "edit",
        "approve",
        "publish",
        "analyze",
        "admin",
    }


def test_a_viewer_is_not_widened_by_the_migration(
    user: Any, other_user: Any, plans: dict[str, Any]
) -> None:
    workspace = _pre_migration_workspace(user, plans["pro"])
    Membership.objects.create(
        user=other_user, workspace=workspace, role=Role.VIEWER, permissions=[]
    )

    call_command("backfill_organizations", stdout=StringIO())

    viewer = Membership.objects.get(user=other_user, workspace=workspace)
    assert set(viewer.permissions) == {"view", "analyze"}
    assert "publish" not in viewer.permissions


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
    )

    with time_machine.travel(timezone.now() + dt.timedelta(minutes=2), tick=False):
        publishing.publish_due()

    post.refresh_from_db()
    assert post.status == PostStatus.PUBLISHED
