"""Workspace provisioning (implementation.md Phase 2.3)."""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import User
from billing.models import Plan
from workspaces.models import (
    ApprovalChain,
    Membership,
    Organization,
    OrganizationMembership,
    Role,
    Workspace,
    WorkspaceStatus,
    permissions_for,
)
from workspaces.services.approvals import DEFAULT_CHAIN_NAME

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

    The plan sits on the **organization** and nowhere else (L-1, P0-56):
    entitlement accounting pools at the company, and the workspace copy that
    existed during the migration is dropped.
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
    )
    Membership.objects.create(
        user=user,
        workspace=workspace,
        role=Role.OWNER,
        permissions=sorted(permissions_for(Role.OWNER)),
    )
    _seed_approval_chain(workspace)
    return workspace


def _seed_approval_chain(workspace: Workspace) -> ApprovalChain:
    """Every workspace gets a default chain at birth (P2-04).

    Non-blocking, which is the solo user's flow: whoever schedules a post is
    the one approving it, recorded as an `APPROVE` action either way (L-2).
    Turning on a separate reviewer is one PATCH away.

    Created here rather than lazily so that "what is this workspace's review
    flow" always has an answer written down. `approvals.default_chain` heals a
    missing one anyway — a workspace with no chain would otherwise be a state
    every caller had to invent a response to — but relying on the heal would
    mean the chain's `created_at` recorded whenever someone first asked.
    """
    return ApprovalChain.objects.create(
        workspace=workspace, name=DEFAULT_CHAIN_NAME, is_default=True, blocks_publish=False
    )


@transaction.atomic
def provision_extra_workspace(*, user: User, name: str) -> Workspace:
    """A second (or twentieth) brand inside an organization the user already
    belongs to (P0-46, P0-18).

    Distinct from `provision_workspace`, which creates the organization too.
    Splitting them keeps the registration path from having to ask "does an org
    exist already?" — a question with three answers and only one correct one.

    **The Stripe quantity is incremented outside this transaction**, by the
    caller of `billing.services.subscriptions.sync_workspace_quantity`. If that
    fails the workspace stays `PENDING_BILLING` and read-only rather than being
    refused: the webhook is the source of truth for what was granted, and a
    workspace the customer asked for and cannot see is worse than one they can
    see and cannot yet write to.
    """
    organization = (
        Organization.objects.filter(memberships__user=user).order_by("created_at").first()
    )
    if organization is None:
        raise NoOrganizationError("This account has no organization to create a workspace in.")

    workspace = Workspace.objects.create(
        organization=organization,
        name=name,
        slug=Workspace.unique_slug(name),
        status=WorkspaceStatus.PENDING_BILLING,
    )
    Membership.objects.create(
        user=user,
        workspace=workspace,
        role=Role.OWNER,
        permissions=sorted(permissions_for(Role.OWNER)),
    )
    _seed_approval_chain(workspace)
    return workspace


class NoOrganizationError(Exception):
    """Unreachable through registration, which always makes one — but a
    permission check may not assume away a state the database allows."""
