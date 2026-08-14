"""Role permission classes (design.md §8.8).

The role table's structural counterpart to `billing.permissions.HasFeature`: a
factory returning a class, for the same reason — DRF instantiates whatever is
in `permission_classes`, and anything else has to fake being a class.

**403, not 402.** `HasFeature` deliberately raises `FeatureNotAvailable` for a
402-with-upgrade payload, because a plan gate is an entitlement failure (design
A2). A role gate is not: no upgrade fixes "you are a CONTRIBUTOR, not an
ADMIN", so `HasRole` returns a plain `False` and lets DRF's ordinary 403 stand.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from common.workspaces import active_workspace
from workspaces.models import ROLE_RANK, Membership


def caller_role(request: Request) -> str | None:
    """The caller's role in their active workspace, or `None` if they are
    somehow authenticated without a membership — unreachable through normal
    signup (`provision_workspace` creates the OWNER membership in the same
    transaction) but not a case a permission check may assume away."""
    if not request.user or not request.user.is_authenticated:
        return None
    membership = (
        Membership.objects.filter(user=request.user, workspace=active_workspace(request))
        .values_list("role", flat=True)
        .first()
    )
    return membership


def role_at_least(role: str | None, minimum: str) -> bool:
    """`True` when `role` is at least as senior as `minimum` — lower
    `ROLE_RANK` is more senior, so this is a `<=`, not a `>=`."""
    if role is None:
        return False
    return ROLE_RANK[role] <= ROLE_RANK[minimum]


def HasRole(minimum: str) -> type[BasePermission]:  # noqa: N802 — reads as a class
    """`permission_classes = [IsAuthenticated, HasRole(Role.ADMIN)]`."""

    class _HasRole(BasePermission):
        def has_permission(self, request: Request, view: Any) -> bool:
            return role_at_least(caller_role(request), minimum)

    _HasRole.__name__ = f"HasRole({minimum!r})"
    return _HasRole
