"""Workspace scoping (design.md §11, implementation.md §4.6).

Tenancy leakage is a security bug, so every workspace-scoped queryset goes
through one place. Cross-workspace access must 404 rather than 403 (design.md
A9) — a 403 would confirm the object exists.

`test_cross_workspace_access_returns_404_on_every_viewset` (Phase 4) walks the
router and asserts every registered ViewSet is covered by this.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from rest_framework.exceptions import NotAuthenticated


class WorkspaceScopedQuerySetMixin:
    """Filters the queryset to the request's active workspace.

    Set `workspace_field` when the relation is not a direct `workspace` FK
    (e.g. "post__workspace" for PostTarget).

    `request.workspace` is resolved by `common.workspaces.active_workspace` —
    that is the only resolver, and an app that filters memberships itself puts
    its tenancy decision outside what the Phase 4 sweep can walk.
    """

    workspace_field: str = "workspace"

    def get_workspace(self) -> Any:
        """The workspace this request may see.

        An explicit `request.workspace` wins — that is how a test, or a later
        workspace-switcher, pins the scope. Otherwise it resolves through the
        one resolver, so a view gets correct scoping by inheriting this rather
        than by remembering to set an attribute.
        """
        if hasattr(self.request, "workspace"):  # type: ignore[attr-defined]
            workspace = self.request.workspace  # type: ignore[attr-defined]
            if workspace is None:
                # Set but empty is a scoping bug, not a cue to resolve: returning
                # everything here is the failure this mixin exists to prevent.
                raise NotAuthenticated("No active workspace for this request.")
            return workspace

        from common.workspaces import active_workspace

        return active_workspace(self.request)  # type: ignore[attr-defined]

    def get_queryset(self) -> QuerySet[Any]:
        queryset: QuerySet[Any] = super().get_queryset()  # type: ignore[misc]
        return queryset.filter(**{self.workspace_field: self.get_workspace()})
