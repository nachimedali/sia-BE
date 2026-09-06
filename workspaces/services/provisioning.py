"""Workspace provisioning (implementation.md Phase 2.3)."""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import User
from billing.models import Plan
from workspaces.models import (
    Membership,
    Organization,
    OrganizationMembership,
    Role,
    Workspace,
    permissions_for,
)

logger = logging.getLogger(__name__)

FREE_PLAN_CODE = "free"


def default_workspace_name(email: str) -> str:
    local = email.split("@")[0]
    cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return (cleaned.title() or "My") + " Workspace"


@transaction.atomic
def provision_workspace(user: User, name: str | None = None) -> Workspace:
    """Creates an organization, a workspace inside it, and both memberships.

    One transaction on purpose: a user with no workspace, a workspace with no
    owner membership, or — since P0-45 — a workspace with no organization is a
    broken account that every later request would have to defend against.

    **The organization is created here, not backfilled** (P0-45). Registration
    is the one moment where the whole hierarchy can be built atomically;
    everything after it is repair. Existing single-workspace accounts from
    before this change are handled by `backfill_organizations` (P0-52), which
    is a separate, resumable command precisely because it cannot borrow this
    transaction.

    The plan sits on **both** rows during expand. Entitlement accounting pools
    at the organization (L-1), but the resolver still reads the workspace copy
    until P0-55 cuts reads over — writing both is what makes that switch a
    one-line change rather than a migration.
    """
    workspace_name = name or default_workspace_name(user.email)

    free_plan = Plan.objects.filter(code=FREE_PLAN_CODE).first()
    if free_plan is None:
        # Not fatal — the account is still usable and Phase 3's grant task will
        # reconcile it — but it means seed_plans was never run.
        logger.error(
            "Free plan missing; workspace provisioned without a plan", extra={"user_id": user.pk}
        )

    organization = Organization.objects.create(
        name=workspace_name,
        slug=Organization.unique_slug(workspace_name),
        owner=user,
        plan=free_plan,
    )
    # `OWNER` is not assignable on a membership — ownership is
    # `Organization.owner`, a single row, so it cannot drift out of sync with a
    # membership table. `ADMIN` is the highest assignable rank and carries the
    # same permission set.
    OrganizationMembership.objects.create(organization=organization, user=user, role=Role.ADMIN)

    workspace = Workspace.objects.create(
        organization=organization,
        name=workspace_name,
        slug=Workspace.unique_slug(workspace_name),
        owner=user,
        plan=free_plan,
    )
    Membership.objects.create(
        user=user,
        workspace=workspace,
        role=Role.OWNER,
        permissions=sorted(permissions_for(Role.OWNER)),
    )
    return workspace
