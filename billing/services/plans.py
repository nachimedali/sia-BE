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
