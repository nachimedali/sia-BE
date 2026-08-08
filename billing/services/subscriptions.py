"""Subscription lifecycle (design.md §8.1, I10, implementation.md Phase 3.5-3.6).

**Stripe is the source of truth for subscription state.** Nothing here decides a
workspace is paid because a checkout session was created — it decides that when
the webhook says so. A user who abandons the Stripe page, or whose card is
declined after the redirect, must not end up entitled.

`Workspace.plan` is what `Entitlements` resolves from, so every path that
changes billing state ends by writing that field and letting the cache key
rotate with it.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from billing.gateways.base import CheckoutSession, PortalSession
from billing.gateways.stripe import get_billing_gateway
from billing.models import (
    CreditReason,
    Plan,
    Subscription,
    SubscriptionStatus,
    VideoReason,
)
from billing.services import ledger
from common.exceptions import OCCSError, StateConflict
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

FREE_PLAN_CODE = "free"

BILLING_CYCLES = ("monthly", "annual")

# Stripe status → ours. Anything unmapped is treated as not entitling, which is
# the safe direction: a new Stripe status should not silently grant access.
STRIPE_STATUS = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.INCOMPLETE,
    "unpaid": SubscriptionStatus.UNPAID,
}


def free_plan() -> Plan:
    plan = Plan.objects.filter(code=FREE_PLAN_CODE).first()
    if plan is None:
        raise RuntimeError("The 'free' plan is missing. Run `manage.py seed_plans`.")
    return plan


# -----------------------------------------------------------------------------
# Checkout
# -----------------------------------------------------------------------------
def start_checkout(
    workspace: Workspace, *, plan_code: str, cycle: str, success_url: str, cancel_url: str
) -> CheckoutSession:
    if cycle not in BILLING_CYCLES:
        raise OCCSError(f"Billing cycle must be one of {BILLING_CYCLES}.", code="invalid_cycle")

    plan = Plan.objects.filter(code=plan_code, is_public=True).first()
    if plan is None or plan.code == FREE_PLAN_CODE:
        raise OCCSError("That plan cannot be subscribed to.", code="invalid_plan")

    price_id = plan.stripe_price_id_annual if cycle == "annual" else plan.stripe_price_id_monthly
    if not price_id:
        # A plan with no Stripe price is a configuration error, not a user error.
        logger.error("plan has no Stripe price id", extra={"plan": plan.code, "cycle": cycle})
        raise OCCSError("This plan is not available for purchase yet.", code="plan_not_purchasable")

    if Subscription.current_for(workspace) is not None:
        raise StateConflict(
            "This workspace already has an active subscription. "
            "Use the billing portal to change plans.",
            code="already_subscribed",
        )

    # One trial per workspace (§8.1). Stripe enforces the same rule per billing
    # identity, which catches the second workspace on one card.
    trial_days = plan.trial_days if workspace.trial_ends_at is None else 0

    session = get_billing_gateway().create_checkout_session(
        workspace_id=workspace.pk,
        customer_id=workspace.stripe_customer_id or None,
        customer_email=workspace.owner.email,
        price_id=price_id,
        trial_days=trial_days,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    logger.info(
        "checkout session created",
        extra={"workspace_id": workspace.pk, "plan": plan.code, "cycle": cycle},
    )
    return session


def open_portal(workspace: Workspace, *, return_url: str) -> PortalSession:
    if not workspace.stripe_customer_id:
        raise StateConflict(
            "This workspace has never been billed, so it has no portal.",
            code="no_billing_customer",
        )
    return get_billing_gateway().create_portal_session(
        customer_id=workspace.stripe_customer_id, return_url=return_url
    )


# -----------------------------------------------------------------------------
# Applying state (called by the webhook, never by a request)
# -----------------------------------------------------------------------------
@transaction.atomic
def apply_subscription_state(
    workspace: Workspace,
    *,
    plan: Plan,
    stripe_subscription_id: str,
    status: str,
    period_start: dt.datetime | None,
    period_end: dt.datetime | None,
    cancel_at_period_end: bool,
) -> Subscription:
    """Upserts the subscription and syncs the workspace onto its plan.

    Idempotent on `stripe_subscription_id`: Stripe sends `customer.subscription.
    updated` for a great many reasons and often more than once per change.
    """
    subscription, created = Subscription.objects.update_or_create(
        stripe_subscription_id=stripe_subscription_id,
        defaults={
            "workspace": workspace,
            "plan": plan,
            "status": status,
            "period_start": period_start,
            "period_end": period_end,
            "cancel_at_period_end": cancel_at_period_end,
        },
    )

    if subscription.is_live:
        _move_to_plan(workspace, plan, trialing=status == SubscriptionStatus.TRIALING)
        if created:
            _grant_period_allowances(
                workspace,
                plan,
                credit_reason=(
                    CreditReason.TRIAL_GRANT
                    if status == SubscriptionStatus.TRIALING
                    else CreditReason.MONTHLY_GRANT
                ),
            )
    else:
        downgrade_to_free(workspace, reason=f"subscription {status.lower()}")

    logger.info(
        "subscription state applied",
        extra={
            "workspace_id": workspace.pk,
            "plan": plan.code,
            "status": status,
            # Not "created": that is a reserved LogRecord attribute and logging
            # raises rather than shadowing it.
            "is_new": created,
        },
    )
    return subscription


def _move_to_plan(workspace: Workspace, plan: Plan, *, trialing: bool) -> None:
    fields = ["plan", "updated_at"]
    workspace.plan = plan

    if trialing and workspace.trial_ends_at is None:
        workspace.trial_ends_at = timezone.now() + dt.timedelta(days=plan.trial_days)
        fields.append("trial_ends_at")

    workspace.save(update_fields=fields)


def _grant_period_allowances(
    workspace: Workspace, plan: Plan, *, credit_reason: str = CreditReason.MONTHLY_GRANT
) -> None:
    ledger.grant_credits(
        workspace,
        plan.monthly_ai_credits,
        reason=credit_reason,
        rollover=bool(plan.feature("credits_rollover")),
        note=f"{plan.code} allowance",
    )
    ledger.grant_video_units(
        workspace,
        plan.included_videos,
        reason=VideoReason.MONTHLY_GRANT,
        note=f"{plan.code} allowance",
    )


# -----------------------------------------------------------------------------
# Downgrade — I10
# -----------------------------------------------------------------------------
@transaction.atomic
def downgrade_to_free(workspace: Workspace, *, reason: str = "trial expired") -> Workspace:
    """Moves a workspace to Free **without destroying anything** (I10).

    The rule is that a lapsed subscription is a billing event, not a data event.
    A user who forgets a card and comes back a week later must find their posts,
    products and connected accounts where they left them.

    Later phases attach their own over-cap handling here rather than in the
    webhook, so there is one answer to "what happens on downgrade":

    * Phase 4 — posts scheduled beyond the Free horizon → `PAUSED`
    * Phase 9 — connected accounts beyond `max_social_accounts` → `OVER_LIMIT`

    Both mark; neither deletes.
    """
    plan = free_plan()
    if workspace.plan_id == plan.pk:
        return workspace

    previous = workspace.plan.code if workspace.plan else "none"
    workspace.plan = plan
    workspace.save(update_fields=["plan", "updated_at"])

    # The Free allowance replaces the paid one; credits already spent stay spent.
    ledger.grant_credits(
        workspace,
        plan.monthly_ai_credits,
        reason=CreditReason.MONTHLY_GRANT,
        rollover=False,
        note=f"downgraded from {previous}: {reason}",
    )
    ledger.grant_video_units(
        workspace, plan.included_videos, note=f"downgraded from {previous}: {reason}"
    )

    logger.info(
        "workspace downgraded to free",
        extra={"workspace_id": workspace.pk, "from_plan": previous, "reason": reason},
    )
    return workspace


# -----------------------------------------------------------------------------
# Monthly grant
# -----------------------------------------------------------------------------
def current_period_start(workspace: Workspace) -> dt.datetime:
    """When the workspace's current allowance period began.

    Paid workspaces follow Stripe's period so the allowance lines up with what
    was invoiced. Free workspaces have no invoice, so the period is anchored on
    the day they were created — arbitrary, but stable, and it spreads the grant
    sweep across the month instead of stampeding everyone on the 1st.
    """
    subscription = Subscription.current_for(workspace)
    if subscription is not None and subscription.period_start is not None:
        return subscription.period_start

    now = timezone.now()
    anchor_day = workspace.created_at.day
    # Clamp for short months: a workspace created on the 31st renews on the 28th
    # in February rather than skipping the month entirely.
    day = min(anchor_day, _days_in_month(now.year, now.month))
    candidate = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if candidate > now:
        month = now.month - 1 or 12
        year = now.year - 1 if now.month == 1 else now.year
        candidate = candidate.replace(
            year=year, month=month, day=min(anchor_day, _days_in_month(year, month))
        )
    return max(candidate, workspace.created_at)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def grant_due_period_allowances() -> int:
    """Grants any workspace that has not been granted this period.

    Derived from the ledger rather than from a "last granted" column: the query
    is the same thing the invariant says, so the task is idempotent by
    construction and safe to run as often as you like.
    """
    from billing.models import CreditLedger

    granted = 0
    for workspace in Workspace.objects.select_related("plan").iterator():
        plan = workspace.plan
        if plan is None:
            continue

        period_start = current_period_start(workspace)
        already = CreditLedger.objects.filter(
            workspace=workspace,
            reason__in=(CreditReason.MONTHLY_GRANT, CreditReason.TRIAL_GRANT),
            created_at__gte=period_start,
        ).exists()
        if already:
            continue

        _grant_period_allowances(workspace, plan)
        granted += 1

    return granted


def expire_lapsed_trials() -> int:
    """Downgrades workspaces whose trial ran out without converting.

    The resolver already refuses entitlements for these (§8.1), so this is
    about making the stored state match what is actually being served rather
    than about closing a gate.
    """
    now = timezone.now()
    candidates = Workspace.objects.filter(trial_ends_at__lt=now).exclude(plan__code=FREE_PLAN_CODE)

    downgraded = 0
    for workspace in candidates.select_related("plan").iterator():
        if Subscription.current_for(workspace) is not None:
            continue
        downgrade_to_free(workspace, reason="trial expired")
        downgraded += 1
    return downgraded
