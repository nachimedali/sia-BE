"""Workspace provisioning (implementation.md Phase 2.3)."""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import User
from billing.models import Plan
from workspaces.models import Membership, Role, Workspace

logger = logging.getLogger(__name__)

FREE_PLAN_CODE = "free"


def default_workspace_name(email: str) -> str:
    local = email.split("@")[0]
    cleaned = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return (cleaned.title() or "My") + " Workspace"


@transaction.atomic
def provision_workspace(user: User, name: str | None = None) -> Workspace:
    """Creates a workspace, its OWNER membership, and assigns the Free plan.

    One transaction on purpose: a user with no workspace, or a workspace with no
    owner membership, is a broken account that every later request would have to
    defend against.
    """
    workspace_name = name or default_workspace_name(user.email)

    free_plan = Plan.objects.filter(code=FREE_PLAN_CODE).first()
    if free_plan is None:
        # Not fatal — the account is still usable and Phase 3's grant task will
        # reconcile it — but it means seed_plans was never run.
        logger.error(
            "Free plan missing; workspace provisioned without a plan", extra={"user_id": user.pk}
        )

    workspace = Workspace.objects.create(
        name=workspace_name,
        slug=Workspace.unique_slug(workspace_name),
        owner=user,
        plan=free_plan,
    )
    Membership.objects.create(user=user, workspace=workspace, role=Role.OWNER)
    return workspace
