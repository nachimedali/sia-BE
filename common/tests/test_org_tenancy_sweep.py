"""The organization variant of the tenancy sweep (P0-61).

The existing sweep in `content/tests/test_api.py` proves that one workspace
cannot read another's rows. This one adds the dimension above it: **two
workspaces in two different organizations**, which is the shape a real group
account has and the shape the old `active_workspace` fallback could not see.

It also fails any ViewSet that declares no scoping at all — the failure mode
that matters most, because a ViewSet added without the mixin looks completely
normal until someone with two memberships opens it.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from rest_framework.viewsets import GenericViewSet

from common.mixins import WorkspaceScopedQuerySetMixin
from config.api_urls import router
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

#: ViewSets that answer for the caller rather than for a workspace-scoped row,
#: and therefore have nothing to scope. Named explicitly so adding one is a
#: deliberate edit here rather than a silent exemption.
UNSCOPED_BY_DESIGN: frozenset[str] = frozenset()


def test_every_registered_viewset_declares_a_scope() -> None:
    """A ViewSet without the mixin is not obviously broken — it is broken only
    for accounts with more than one workspace, which is precisely the
    population this phase creates."""
    unscoped = [
        basename
        for _prefix, viewset, basename in router.registry
        if basename not in UNSCOPED_BY_DESIGN
        and issubclass(viewset, GenericViewSet)
        and not issubclass(viewset, WorkspaceScopedQuerySetMixin)
    ]

    assert unscoped == [], (
        f"These ViewSets declare no workspace scope: {', '.join(unscoped)}. "
        "Add WorkspaceScopedQuerySetMixin, or name them in UNSCOPED_BY_DESIGN "
        "with a reason."
    )


def test_a_second_organizations_workspace_is_invisible(auth_client: Any, workspace: Any) -> None:
    """Cross-*organization*, not merely cross-workspace. The caller is a
    legitimate user of their own org and a stranger to this one."""
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")

    assert theirs.organization != workspace.organization

    response = auth_client.get("/api/v1/workspaces/")

    assert [row["id"] for row in response.json()] == [workspace.pk]


def test_naming_another_organizations_workspace_is_404(auth_client: Any) -> None:
    """404, never 403 (Part 7 rule 3), and now across three dimensions rather
    than one."""
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")

    response = auth_client.get("/api/v1/products/", HTTP_X_WORKSPACE_ID=str(theirs.pk))

    assert response.status_code == 404


def test_the_organization_list_never_leaks_another_company(
    auth_client: Any, organization: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    provision_workspace(stranger, name="Another Company")

    rows = auth_client.get("/api/v1/organizations/").json()

    assert [row["id"] for row in rows] == [organization.pk]
