"""The DRF permission gate (design.md §8.8, BUILD-PLAN Phase 0).

Mirrors `billing/tests/test_permissions.py`'s shape for `HasFeature` — the
same reasoning applies: this covers the permission-class gate on its own, in
isolation from the serializer/service/UI layers that could otherwise be the
one that was forgotten.

`test_authority.py` pins the 5x7 derivation table; this file pins what the
gate does with it, including the case the table cannot express — a membership
whose stored permissions disagree with its role preset.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from workspaces.models import Membership, Permission, Role
from workspaces.permissions import HasPermission, member_permissions

pytestmark = pytest.mark.django_db


class AdminOnlyView(APIView):
    permission_classes = (IsAuthenticated, HasPermission(Permission.ADMIN))

    def get(self, request: Any) -> Response:
        return Response({"ok": True})


def _call(view: type[APIView], user: Any) -> Response:
    request = APIRequestFactory().get("/x")
    force_authenticate(request, user=user)
    return view.as_view()(request)


# -----------------------------------------------------------------------------
# HasPermission
# -----------------------------------------------------------------------------
def test_a_holder_passes_the_gate(workspace: Any, user: Any) -> None:
    """`user` is the workspace's OWNER (`provision_workspace`), whose preset
    holds every permission."""
    assert _call(AdminOnlyView, user).status_code == 200


def test_a_non_holder_is_403_not_402(advanced_workspace: Any, contributor_user: Any) -> None:
    """A permission gate is not an entitlement failure (design A2) — no
    upgrade fixes "you do not hold `admin`", so this is DRF's ordinary 403,
    unlike `HasFeature`'s 402."""
    response = _call(AdminOnlyView, contributor_user)

    assert response.status_code == 403
    response.render()
    assert response.data["error"]["code"] == "permission_denied"


def test_anonymous_callers_are_401_not_403(workspace: Any) -> None:
    request = APIRequestFactory().get("/x")
    response = AdminOnlyView.as_view()(request)

    assert response.status_code == 401


def test_a_caller_with_no_membership_holds_nothing(workspace: Any, other_user: Any) -> None:
    """ "No membership" must read as no permissions rather than as the preset
    of some default role — an empty set is the deny, and deriving a default
    would hand a stranger a CONTRIBUTOR's authority.

    Asserted on the helper rather than through a view: `request_workspace`
    refuses before any gate runs for a caller who belongs to no workspace at
    all, so the view path cannot reach the question this test is asking."""
    assert member_permissions(other_user, workspace) == set()


# -----------------------------------------------------------------------------
# permissions is authority; role is display
# -----------------------------------------------------------------------------
def test_stored_permissions_override_the_role_preset(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    """The whole point of the permission set: a CONTRIBUTOR granted `admin`
    holds it. If the gate re-derived from `role` it would deny, and the
    column would be decorative."""
    membership = Membership.objects.get(user=contributor_user, workspace=advanced_workspace)
    membership.permissions = [Permission.ADMIN, Permission.VIEW]
    membership.save(update_fields=["permissions"])

    assert _call(AdminOnlyView, contributor_user).status_code == 200


def test_an_empty_permission_set_derives_from_the_role(
    advanced_workspace: Any, admin_user: Any
) -> None:
    """Rows written before the expand migration carry `[]`. Empty must derive,
    not deny: an empty list is indistinguishable from a working deny, and the
    failure would be a locked-out user rather than a visible error."""
    membership = Membership.objects.get(user=admin_user, workspace=advanced_workspace)
    assert membership.permissions == []
    assert membership.role == Role.ADMIN

    assert member_permissions(admin_user, advanced_workspace) == {
        "view",
        "comment",
        "edit",
        "approve",
        "publish",
        "analyze",
        "admin",
    }
    assert _call(AdminOnlyView, admin_user).status_code == 200


def test_member_permissions_is_scoped_to_the_workspace_asked_about(
    advanced_workspace: Any, contributor_user: Any, other_user: Any
) -> None:
    """A membership in one brand grants nothing in another — the same tenancy
    boundary the querysets enforce, asked of the permission helper."""
    from workspaces.services.provisioning import provision_workspace

    elsewhere = provision_workspace(other_user, name="Rival Studio")

    assert member_permissions(contributor_user, advanced_workspace)
    assert member_permissions(contributor_user, elsewhere) == set()
