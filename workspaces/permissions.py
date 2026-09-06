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

from common.workspaces import request_workspace
from workspaces.models import ROLE_RANK, Membership, permissions_for


def caller_role(request: Request) -> str | None:
    """The caller's role in their active workspace, or `None` if they are
    somehow authenticated without a membership — unreachable through normal
    signup (`provision_workspace` creates the OWNER membership in the same
    transaction) but not a case a permission check may assume away."""
    if not request.user or not request.user.is_authenticated:
        return None
    membership = (
        Membership.objects.filter(user=request.user, workspace=request_workspace(request))
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


def caller_permissions(request: Request) -> set[str]:
    """The caller's permission set in the requested workspace (P0-11).

    **Dual-read.** `Membership.permissions` is authority once it is populated;
    where it is still empty — every row written before the expand migration —
    the answer is derived from `role` through the same pure function the
    backfill uses. Deriving rather than treating empty as "no permissions"
    matters: an empty list is indistinguishable from a working deny, and the
    failure would be a locked-out user rather than a visible error.

    The derivation is exhaustively pinned in `test_authority.py` (5 roles x 7
    permissions, none sampled), because a migration that silently *widens*
    access is the worst outcome available here.
    """
    if not request.user or not request.user.is_authenticated:
        return set()
    membership = (
        Membership.objects.filter(user=request.user, workspace=request_workspace(request))
        .values_list("role", "permissions")
        .first()
    )
    if membership is None:
        return set()
    role, stored = membership
    return set(stored) if stored else permissions_for(role)


def HasPermission(permission: str) -> type[BasePermission]:  # noqa: N802 — reads as a class
    """`permission_classes = [IsAuthenticated, HasPermission("approve")]`.

    Replaces `HasRole`, which asked a coarser question — "are you at least an
    ADMIN?" — that had to be re-derived every time a role's meaning shifted.
    The five roles survive as seeded presets over this set: `role` is display,
    `permissions` is authority.

    **403, not 402**, for the same reason `HasRole` was: no upgrade fixes "you
    do not hold `approve`", so this returns a plain `False` and lets DRF's
    ordinary 403 stand.
    """

    class _HasPermission(BasePermission):
        def has_permission(self, request: Request, view: Any) -> bool:
            return permission in caller_permissions(request)

    _HasPermission.__name__ = f"HasPermission({permission!r})"
    return _HasPermission


def HasRole(minimum: str) -> type[BasePermission]:  # noqa: N802 — reads as a class
    """`permission_classes = [IsAuthenticated, HasRole(Role.ADMIN)]`.

    **Shim, kept only through dual-read** (P0-11). Every gate should move to
    `HasPermission`; this stays so the move can be one view at a time rather
    than one deploy, and P0-56 deletes it along with `ROLE_RANK`.
    """

    class _HasRole(BasePermission):
        def has_permission(self, request: Request, view: Any) -> bool:
            return role_at_least(caller_role(request), minimum)

    _HasRole.__name__ = f"HasRole({minimum!r})"
    return _HasRole
