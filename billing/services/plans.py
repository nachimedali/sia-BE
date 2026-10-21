"""The one writer of a plan assignment (P0-53 … P0-56).

**Billing pools at the organization** (L-1), so `Organization.plan` is the only
place a plan lives and `Entitlements` is the only thing that reads it. The
migration that moved it up from `Workspace` is contracted: there is no shadow
copy left to disagree with, which is why `parity_drift` — the metric that
watched the two columns during dual-write — is gone rather than kept as a
permanently-empty check.

One function rather than direct assignment at each call site, because "assign a
plan" is about to acquire company: the Stripe quantity update, the add-on sweep
and the downgrade path all want the same before/after hook, and three call
sites writing one column each is how drift starts.
"""

from __future__ import annotations

import logging

from django.db import transaction

from billing.models import Plan
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

#: Named only for the bug path: an upgrade prompt must name *a* plan even when
#: the catalogue has no public paid row to suggest (design.md A2).
FALLBACK_SUGGESTION = "pro"


def default_plan() -> Plan:
    """The plan new organizations start on and lapsed subscriptions return
    to: the row marked `is_default` in admin.

    Missing is a deployment error, raised rather than papered over: inventing
    an in-memory plan would serve quotas nobody can audit.
    """
    plan = Plan.objects.filter(is_default=True).first()
    if plan is None:
        raise RuntimeError("No plan is marked as the default. Run `manage.py seed_plans`.")
    return plan


def suggest_upgrade(current: Plan, feature_key: str | None = None) -> str:
    """The code of the cheapest public plan above `current` that unlocks
    `feature_key` (or any plan above it, for a quota).

    Read from the catalogue rather than named in code, so renaming or adding
    a package changes every upgrade prompt with no deploy. Falls back to the
    dearest public plan, then to `FALLBACK_SUGGESTION` on an empty catalogue.
    """
    public = list(
        Plan.objects.filter(is_public=True).order_by("price_monthly_cents", "sort_order", "id")
    )
    above = [plan for plan in public if plan.price_monthly_cents > current.price_monthly_cents]
    for plan in above:
        if feature_key is None or plan.feature(feature_key):
            return plan.code
    if public:
        return public[-1].code
    return FALLBACK_SUGGESTION


@transaction.atomic
def set_plan(workspace: Workspace, plan: Plan | None) -> None:
    """Assigns `plan` to the organization above `workspace`.

    Takes a workspace rather than an organization because every caller has one
    — a checkout session, a webhook, a downgrade — and making each of them walk
    up the tree is how one of them eventually forgets and writes the wrong row.
    """
    organization = workspace.organization
    if organization.plan_id == (plan.pk if plan else None):
        return

    organization.plan = plan
    organization.save(update_fields=["plan", "updated_at"])
    logger.info(
        "plan assigned",
        extra={
            "organization_id": organization.pk,
            "workspace_id": workspace.pk,
            "plan": plan.code if plan else None,
        },
    )
