"""Plan assignment during the org migration (P0-53, P0-54).

**One writer, both rows.** `Workspace.plan` is what the resolver reads today;
`Organization.plan` is what it reads after P0-55. Between those two points every
assignment has to land on both, and it has to land through one function — two
call sites writing one column each is how drift starts, and drift here means a
customer entitled to different things depending on which read won.

`parity_drift` is the metric P0-54 asks for: dual-write is not "done" because
the code writes twice, it is done when a full billing cycle of production data
says the two agree.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.db.models import F, Q

from billing.models import Plan
from workspaces.models import Workspace

logger = logging.getLogger(__name__)


@transaction.atomic
def set_plan(workspace: Workspace, plan: Plan | None) -> None:
    """Assigns `plan` to a workspace and to the organization above it.

    The organization is authoritative for *billing* (L-1: entitlement
    accounting pools at the org), so a workspace whose org disagrees is a bug
    even while the resolver still reads the workspace copy — which is why this
    writes both rather than waiting for the cut-over to start caring.
    """
    workspace.plan = plan
    workspace.save(update_fields=["plan", "updated_at"])

    organization = workspace.organization
    if organization is None:
        # Pre-backfill row. `backfill_organizations` will create the org and
        # copy the plan; nothing is lost by not having one to write to yet.
        logger.info("workspace has no organization yet", extra={"workspace_id": workspace.pk})
        return
    if organization.plan_id != (plan.pk if plan else None):
        organization.plan = plan
        organization.save(update_fields=["plan", "updated_at"])


def parity_drift() -> list[dict[str, Any]]:
    """Workspaces whose plan disagrees with their organization's (P0-54).

    **Reports, never repairs** (Part 7 rule 7). A repair here would hide the
    bug that caused the divergence, and the divergence is the only evidence
    that a write path bypassed `set_plan`.
    """
    rows = (
        Workspace.objects.exclude(organization__isnull=True)
        .exclude(plan_id=F("organization__plan_id"))
        .exclude(Q(plan__isnull=True) & Q(organization__plan__isnull=True))
        .values("pk", "plan_id", "organization_id", "organization__plan_id")
    )
    drift = [
        {
            "workspace": row["pk"],
            "workspace_plan": row["plan_id"],
            "organization": row["organization_id"],
            "organization_plan": row["organization__plan_id"],
        }
        for row in rows
    ]
    if drift:
        logger.warning("plan parity drift", extra={"count": len(drift)})
    return drift
