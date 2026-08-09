"""Resolving the workspace a request acts on (design.md §11).

One resolver, so `WorkspaceScopedQuerySetMixin` and the hand-written APIViews
answer the question the same way. The alternative — each app filtering
memberships itself — puts tenancy decisions outside the one place the Phase 4
test `test_cross_workspace_access_returns_404_on_every_viewset` can walk.
"""

from __future__ import annotations

import contextlib
from typing import Any

from django.db.models import Model
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
        # `owner` joins because both checkout flows read `owner.email` to hand
        # Stripe a billing identity, and it is one row either way.
        .select_related("plan", "category", "owner")
        .order_by("created_at")
        .first()
    )
    if workspace is None:
        raise OCCSError("This account has no workspace.", code="no_workspace")

    request._active_workspace = workspace  # type: ignore[attr-defined]
    return workspace


def scope_related_field_to_workspace(
    field: Any, request: Request | None, model: type[Model]
) -> None:
    """Restricts a `PrimaryKeyRelatedField`'s (or, for `many=True`, the
    wrapping `ManyRelatedField`'s) choices to the caller's own workspace —
    the one shared shape behind every "don't let one workspace reference
    another's row" serializer field in this codebase.

    Falls back to an empty queryset rather than raising when the workspace
    cannot be resolved — schema generation instantiates every serializer
    with a request that carries no authenticated user, and that has to
    produce a schema, not a 401. `many=True` makes the field a
    `ManyRelatedField` wrapping the real `PrimaryKeyRelatedField` as
    `child_relation` — the queryset lives on the child, not the wrapper (DRF
    has no `.queryset` on `ManyRelatedField`).
    """
    queryset = model._default_manager.none()
    if request is not None:
        with contextlib.suppress(OCCSError):
            queryset = model._default_manager.filter(workspace=active_workspace(request))
    target = getattr(field, "child_relation", field)
    target.queryset = queryset
