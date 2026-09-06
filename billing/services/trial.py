"""The quota trial that replaced Free-forever (L-4, P0-20, P0-21).

A real seeded plan row: one workspace, N posts, **no expiry and no card**. Not
a clock — that is what makes it different from the 7-day plan trials that still
exist for Pro and Advanced, and why `Organization.trial_posts_used` is a
counter rather than a date.

**Pooled at the organization** (L-1). A 6-post package is 6 posts across the
whole company, not 6 per brand, so the counter lives on the org and the lock is
taken there.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from billing.services.entitlements import entitlements_for
from common.exceptions import QuotaExceeded

logger = logging.getLogger(__name__)

#: The seeded plan whose quota this counts. Read from the plan row rather than
#: hardcoded anywhere — this constant only names *which* row.
TRIAL_PLAN_CODE = "trial"


def is_on_trial(workspace: Any) -> bool:
    return entitlements_for(workspace).plan.code == TRIAL_PLAN_CODE


@transaction.atomic
def consume_trial_post(workspace: Any) -> int:
    """Spends one trial post, or raises 402 with an upgrade.

    `select_for_update` on the **organization** row, not the workspace: the
    quota is pooled, so two brands scheduling simultaneously must contend for
    one lock. Without it both read the last remaining post, both pass, and the
    company publishes one more than it bought.

    Returns the number used after this call. A no-op — returning 0 — for any
    plan that is not the trial, so callers need not ask first.
    """
    entitlements = entitlements_for(workspace)
    if entitlements.plan.code != TRIAL_PLAN_CODE:
        return 0

    organization = getattr(workspace, "organization", None)
    if organization is None:
        # Pre-backfill row. The trial cannot be metered without a pool to meter
        # against, and blocking here would lock out an account mid-migration.
        logger.warning("trial post not metered; no organization", extra={"workspace": workspace.pk})
        return 0

    quota = entitlements.quota("trial_post_quota")
    locked = type(organization).objects.select_for_update().get(pk=organization.pk)
    if locked.trial_posts_used >= quota:
        raise QuotaExceeded(
            f"This trial includes {quota} posts, and all of them have been used.",
            detail={"used": locked.trial_posts_used, "quota": quota},
            suggested_plan="pro",
        )

    locked.trial_posts_used += 1
    locked.save(update_fields=["trial_posts_used", "updated_at"])
    organization.trial_posts_used = locked.trial_posts_used
    return int(locked.trial_posts_used)


def trial_posts_remaining(workspace: Any) -> int | None:
    """How many trial posts are left, or `None` when this is not a trial.

    **Never cached** (Part 7 rule 5). It is a balance, and a stale balance is
    how a trial quietly overspends.
    """
    entitlements = entitlements_for(workspace)
    if entitlements.plan.code != TRIAL_PLAN_CODE:
        return None
    organization = getattr(workspace, "organization", None)
    if organization is None:
        return None
    organization.refresh_from_db(fields=["trial_posts_used"])
    return max(0, int(entitlements.quota("trial_post_quota")) - int(organization.trial_posts_used))
