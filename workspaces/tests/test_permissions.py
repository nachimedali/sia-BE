"""The role DRF permission gate (design.md §8.8).

Mirrors `billing/tests/test_permissions.py`'s shape for `HasFeature` — the
same reasoning applies: this covers the permission-class gate on its own, in
isolation from the serializer/service/UI layers that could otherwise be the
one that was forgotten.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from workspaces.models import Role
from workspaces.permissions import HasRole, caller_role, role_at_least

pytestmark = pytest.mark.django_db


class AdminOnlyView(APIView):
    permission_classes = (IsAuthenticated, HasRole(Role.ADMIN))

    def get(self, request: Any) -> Response:
        return Response({"ok": True})


def _call(view: type[APIView], user: Any) -> Response:
    request = APIRequestFactory().get("/x")
    force_authenticate(request, user=user)
    return view.as_view()(request)


# -----------------------------------------------------------------------------
# role_at_least
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        (Role.OWNER, Role.ADMIN, True),  # more senior than the minimum
        (Role.ADMIN, Role.ADMIN, True),  # exactly the minimum
        (Role.EDITOR, Role.ADMIN, False),  # less senior
        (Role.VIEWER, Role.CONTRIBUTOR, False),
        (Role.CONTRIBUTOR, Role.CONTRIBUTOR, True),
        (None, Role.VIEWER, False),  # no membership at all
    ],
)
def test_role_at_least(role: str | None, minimum: str, expected: bool) -> None:
    assert role_at_least(role, minimum) is expected


# -----------------------------------------------------------------------------
# HasRole
# -----------------------------------------------------------------------------
def test_admin_passes_an_admin_gate(workspace: Any, user: Any) -> None:
    """`user` is the workspace's OWNER (`provision_workspace`) — senior to
    ADMIN, so the gate must pass."""
    assert _call(AdminOnlyView, user).status_code == 200


def test_a_lower_role_is_403_not_402(advanced_workspace: Any, contributor_user: Any) -> None:
    """A role gate is not an entitlement failure (design A2) — no upgrade
    fixes "you are a CONTRIBUTOR, not an ADMIN", so this is DRF's ordinary
    403, unlike `HasFeature`'s 402."""
    response = _call(AdminOnlyView, contributor_user)

    assert response.status_code == 403
    response.render()
    assert response.data["error"]["code"] == "permission_denied"


def test_anonymous_callers_are_401_not_403(workspace: Any) -> None:
    request = APIRequestFactory().get("/x")
    response = AdminOnlyView.as_view()(request)

    assert response.status_code == 401


def test_caller_role_resolves_the_active_workspaces_membership(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    request = APIRequestFactory().get("/x")
    force_authenticate(request, user=contributor_user)
    drf_request = APIView().initialize_request(request)
    drf_request.user = contributor_user

    assert caller_role(drf_request) == Role.CONTRIBUTOR
