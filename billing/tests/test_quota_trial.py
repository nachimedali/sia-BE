"""The quota trial (L-4, P0-20, P0-21, P0-22).

Free-forever is replaced by a real seeded plan row: one workspace, N posts, no
expiry, no card. The three properties that make it work rather than merely
exist are a pooled counter, a lock that stops two brands spending the last
post, and a 402 that names an upgrade.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from django.db import connections, transaction
from django.utils import timezone

from billing.models import Plan
from billing.services import trial
from common.exceptions import QuotaExceeded
from content.models import DeliveryMode, Post, PostStatus
from scheduling.services import schedule_post
from workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def trial_workspace(workspace: Any, organization: Any) -> Any:
    plan = Plan.objects.get(code="trial")
    workspace.organization.plan = plan
    workspace.organization.save(update_fields=["plan"])
    organization.plan = plan
    organization.save(update_fields=["plan"])
    return workspace


def _post(workspace: Any, user: Any) -> Post:
    return Post.objects.create(
        workspace=workspace, author=user, master_body="Our seasonal glaze is live today."
    )


# -----------------------------------------------------------------------------
# The seeded row — P0-21
# -----------------------------------------------------------------------------
def test_the_trial_is_a_seeded_row_not_a_special_case(plans: dict[str, Any]) -> None:
    """Part 7 rule 10: no commercial number is hardcoded, so an operator
    retunes the quota in admin like every other one."""
    plan = Plan.objects.get(code="trial")

    assert plan.trial_post_quota > 0
    assert plan.max_workspaces == 1
    # No clock. The quota *is* the trial (L-4) — that is what distinguishes it
    # from Pro's and Advanced's 7-day trials, which still exist.
    assert plan.trial_days == 0


def test_the_paid_plans_are_priced_per_workspace(plans: dict[str, Any]) -> None:
    assert plans["pro"].price_per_workspace_cents > 0
    assert plans["advanced"].price_per_workspace_cents > 0
    assert plans["advanced"].max_workspaces > plans["pro"].max_workspaces


def test_free_survives_alongside_the_trial(plans: dict[str, Any]) -> None:
    """Existing accounts sit on `free`; moving them is a commercial decision,
    not something a seed command should do behind an operator's back."""
    assert Plan.objects.filter(code="free").exists()


# -----------------------------------------------------------------------------
# Metering — P0-20
# -----------------------------------------------------------------------------
def test_scheduling_spends_a_trial_post(trial_workspace: Any, user: Any, organization: Any) -> None:
    schedule_post(
        post=_post(trial_workspace, user),
        delivery_mode=DeliveryMode.REMINDER,
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    organization.refresh_from_db()
    assert organization.trial_posts_used == 1


def test_a_draft_nobody_schedules_costs_nothing(
    trial_workspace: Any, user: Any, organization: Any
) -> None:
    """Metered at the moment a post is committed to going out. Counting drafts
    would make the trial feel smaller than it is."""
    _post(trial_workspace, user)

    organization.refresh_from_db()
    assert organization.trial_posts_used == 0


def test_exhaustion_is_402_with_an_upgrade(
    trial_workspace: Any, user: Any, organization: Any
) -> None:
    quota = Plan.objects.get(code="trial").trial_post_quota
    organization.trial_posts_used = quota
    organization.save(update_fields=["trial_posts_used"])

    with pytest.raises(QuotaExceeded) as caught:
        schedule_post(
            post=_post(trial_workspace, user),
            delivery_mode=DeliveryMode.REMINDER,
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            actor=user,
        )

    assert caught.value.status_code == 402
    assert caught.value.upgrade["suggested_plan"] == "pro"


def test_a_paid_plan_is_never_metered(workspace: Any, user: Any, organization: Any) -> None:
    assert trial.consume_trial_post(workspace) == 0

    organization.refresh_from_db()
    assert organization.trial_posts_used == 0


def test_the_quota_is_pooled_across_the_organization(
    trial_workspace: Any, user: Any, organization: Any
) -> None:
    """L-1: a 6-post package is 6 posts across the whole company, not 6 per
    brand."""
    sibling = Workspace.objects.create(
        organization=organization,
        name="Sibling",
        slug=Workspace.unique_slug("Sibling"),
    )

    trial.consume_trial_post(trial_workspace)
    trial.consume_trial_post(sibling)

    organization.refresh_from_db()
    assert organization.trial_posts_used == 2


def test_remaining_is_computed_not_cached(trial_workspace: Any, organization: Any) -> None:
    """Part 7 rule 5. A stale balance is how a trial quietly overspends."""
    quota = Plan.objects.get(code="trial").trial_post_quota

    assert trial.trial_posts_remaining(trial_workspace) == quota
    trial.consume_trial_post(trial_workspace)
    assert trial.trial_posts_remaining(trial_workspace) == quota - 1


def test_concurrent_spends_cannot_exceed_the_quota(trial_workspace: Any, organization: Any) -> None:
    """Real threads against real Postgres. Without the lock on the org row,
    both readers see the last post available, both pass, and the company
    publishes one more than it bought."""
    quota = Plan.objects.get(code="trial").trial_post_quota
    organization.trial_posts_used = quota - 1
    organization.save(update_fields=["trial_posts_used"])

    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def spend() -> None:
        barrier.wait(timeout=10)
        try:
            with transaction.atomic():
                trial.consume_trial_post(trial_workspace)
            outcomes.append("charged")
        except QuotaExceeded:
            outcomes.append("refused")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=spend) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("charged") == 1
    organization.refresh_from_db()
    assert organization.trial_posts_used == quota


test_concurrent_spends_cannot_exceed_the_quota = pytest.mark.django_db(transaction=True)(
    test_concurrent_spends_cannot_exceed_the_quota
)


# -----------------------------------------------------------------------------
# REMINDER is a capability, not a price point — P0-22
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("code", ["trial", "free", "pro", "advanced"])
def test_reminder_is_available_on_every_plan(
    workspace: Any, user: Any, organization: Any, plans: dict[str, Any], code: str
) -> None:
    """L-4: it is the only path for formats the publishing API cannot reach, so
    gating it would leave those formats unreachable rather than merely unpaid.
    This test is what keeps a future entitlement check out of that branch."""
    plan = Plan.objects.get(code=code)
    workspace.organization.plan = plan
    workspace.organization.save(update_fields=["plan"])
    organization.plan = plan
    organization.save(update_fields=["plan"])

    post = schedule_post(
        post=_post(workspace, user),
        delivery_mode=DeliveryMode.REMINDER,
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.status == PostStatus.REMINDER_ARMED
