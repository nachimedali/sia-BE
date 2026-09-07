"""Permission classes (design.md §8.8, BUILD-PLAN Phase 0).

The authority-set counterpart to `billing.permissions.HasFeature`: a factory
returning a class, for the same reason — DRF instantiates whatever is in
`permission_classes`, and anything else has to fake being a class.

**`Membership.permissions` is the authority; `role` is a display preset.**
`HasRole` and `ROLE_RANK` were the pre-migration gate and are gone (P0-56).
Ranked roles cannot express "may approve but not publish", which approval
chains need by Phase 2, and every attempt to bolt that onto a rank ends in a
second, contradictory ordering.

**403, not 402.** `HasFeature` deliberately raises `FeatureNotAvailable` for a
402-with-upgrade payload, because a plan gate is an entitlement failure (design
A2). A permission gate is not: no upgrade fixes "you do not hold `approve`", so
these return a plain `False` and let DRF's ordinary 403 stand.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from common.workspaces import request_workspace
from workspaces.models import Membership, permissions_for


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
    return member_permissions(request.user, request_workspace(request))


def member_permissions(user: Any, workspace: Any) -> set[str]:
    """`caller_permissions` without a request, for the Celery-preflight recheck
    (`workspaces.services.approvals.ensure_approval_still_valid`), which has an
    actor and a workspace but no HTTP request to read them from.

    Same dual-read, deliberately: two implementations of "what may this member
    do" is how the gate and the recheck end up disagreeing about one person.
    """
    membership = (
        Membership.objects.filter(user=user, workspace=workspace)
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
