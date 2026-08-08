"""Resolving the workspace a request acts on (design.md §11).

One resolver, so `WorkspaceScopedQuerySetMixin` and the hand-written APIViews
answer the question the same way. The alternative — each app filtering
memberships itself — puts tenancy decisions outside the one place the Phase 4
test `test_cross_workspace_access_returns_404_on_every_viewset` can walk.
"""

from __future__ import annotations

from rest_framework.request import Request

from accounts.models import User
from common.exceptions import OCCSError
from workspaces.models import Workspace


def authenticated_user(request: Request) -> User:
    """Narrows request.user for the type checker.

    Every caller sits behind IsAuthenticated, so AnonymousUser is unreachable —
    but DRF types the attribute as the union and mypy is right to insist.
    """
    user = request.user
    if not isinstance(user, User):
        raise OCCSError("Authentication required.", code="not_authenticated")
    return user


def active_workspace(request: Request) -> Workspace:
    """The workspace this request acts on, resolved once per request.

    Phase 2 has exactly one workspace per user, so the owned workspace is
    unambiguous. Multi-workspace switching arrives with invitations in a later
    phase; this is the single place that will need to change.
    """
    cached: Workspace | None = getattr(request, "_active_workspace", None)
    if cached is not None:
        return cached

    workspace = (
        Workspace.objects.filter(memberships__user=authenticated_user(request))
        .select_related("plan", "category")
        .order_by("created_at")
        .first()
    )
    if workspace is None:
        raise OCCSError("This account has no workspace.", code="no_workspace")

    request._active_workspace = workspace  # type: ignore[attr-defined]
    return workspace
