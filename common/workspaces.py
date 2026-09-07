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
from common.exceptions import NotFoundError, OCCSError
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


#: The header the BFF injects (P0-15). Explicit on every request, because the
#: alternative — inferring the workspace server-side — is exactly the silent
#: fallback P0-14 deletes.
WORKSPACE_HEADER = "HTTP_X_WORKSPACE_ID"


def request_workspace(request: Request) -> Workspace:
    """The workspace this request acts on, resolved once per request.

    **This replaces `request_workspace`, which is deleted rather than
    deprecated** (P0-14). The old resolver answered "the caller's oldest
    membership", which is unambiguous only while everyone has exactly one
    workspace. The moment a second exists, that fallback silently routes a
    request at the wrong tenant's data, and nothing in the request says which
    one was meant — a cross-tenant leak that reads as a working feature.

    So: the workspace is named explicitly by `X-Workspace-Id`, injected by
    `proxy.ts`, which is the only thing that reads the session. Two cases
    remain:

    * **header present** — resolved through the caller's own memberships, so
      another tenant's id is a **404, never a 403** (Part 7 rule 3). A 403
      would confirm the workspace exists;
    * **header absent** — permitted only while the caller has exactly one
      workspace, where "which one" has a single answer. With two or more it is
      a 400 naming the header, not a guess.
    """
    cached: Workspace | None = getattr(request, "_request_workspace", None)
    if cached is not None:
        return cached

    user = authenticated_user(request)
    mine = Workspace.objects.filter(memberships__user=user).select_related(
        # The organization's plan and owner join because the entitlement
        # resolver reads the plan on every request and both checkout flows
        # read `owner.email` to hand Stripe a billing identity — one row
        # either way, and both live on the organization since P0-56.
        "category",
        "organization",
        "organization__plan",
        "organization__owner",
    )

    requested = str(request.META.get(WORKSPACE_HEADER, "") or "").strip()
    if requested:
        workspace = mine.filter(pk=requested).first() if requested.isdigit() else None
        if workspace is None:
            raise NotFoundError(
                "No such workspace.", detail={"workspace": requested}, code="workspace_not_found"
            )
    else:
        # Two, not one: we need to know whether there is a *second* before
        # answering, and slicing to two is how that costs one query.
        candidates = list(mine.order_by("created_at")[:2])
        if not candidates:
            raise OCCSError("This account has no workspace.", code="no_workspace")
        if len(candidates) > 1:
            raise OCCSError(
                "This account belongs to more than one workspace; name one with "
                "the X-Workspace-Id header.",
                code="workspace_required",
            )
        workspace = candidates[0]

    request._request_workspace = workspace  # type: ignore[attr-defined]
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
            queryset = model._default_manager.filter(workspace=request_workspace(request))
    target = getattr(field, "child_relation", field)
    target.queryset = queryset
