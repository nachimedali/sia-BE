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
    """

    workspace_field: str = "workspace"

    def get_workspace(self) -> Any:
        workspace = getattr(self.request, "workspace", None)  # type: ignore[attr-defined]
        if workspace is None:
            raise NotAuthenticated("No active workspace for this request.")
        return workspace

    def get_queryset(self) -> QuerySet[Any]:
        queryset: QuerySet[Any] = super().get_queryset()  # type: ignore[misc]
        return queryset.filter(**{self.workspace_field: self.get_workspace()})
