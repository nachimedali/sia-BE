"""The DRF permission gate (design.md §8.1, I5).

I5 wants four independent gates. This covers the second one on its own — the
point being that it blocks without help from a serializer, a task preflight or
the UI, because in production any one of those can be the layer that was
forgotten.
"""

from __future__ import annotations

import pytest
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from billing.permissions import HasFeature
from common.exceptions import FeatureNotAvailable

pytestmark = pytest.mark.django_db


class PlaybookView(APIView):
    permission_classes = (IsAuthenticated, HasFeature("playbook"))

    def get(self, request):
        return Response({"ok": True})


@pytest.fixture
def workspace(plans, user):
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


def _call(user):
    request = APIRequestFactory().get("/playbook/")
    force_authenticate(request, user=user)
    return PlaybookView.as_view()(request)


def test_the_permission_blocks_a_plan_without_the_feature(workspace, user) -> None:
    response = _call(user)

    assert response.status_code == 402
    response.render()
    body = response.data
    assert body["error"]["code"] == "feature_not_available"
    # A2: a 402 always carries the upgrade payload, so the frontend renders an
    # upgrade prompt rather than an error toast.
    assert body["error"]["upgrade"]["suggested_plan"] == "pro"


def test_the_permission_allows_a_plan_with_the_feature(workspace, user, plans) -> None:
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])

    response = _call(user)

    assert response.status_code == 200


def test_the_permission_is_402_not_403(workspace, user) -> None:
    """A 403 would be indistinguishable from a role failure, and the frontend
    has one branch for each (design.md §7.1)."""
    assert _call(user).status_code != 403


def test_anonymous_callers_are_401_not_402(workspace) -> None:
    """Not entitled and not signed in are different problems; only the first is
    solved by paying."""
    request = APIRequestFactory().get("/playbook/")
    response = PlaybookView.as_view()(request)

    assert response.status_code == 401


def test_the_permission_raises_rather_than_returning_false(workspace, user) -> None:
    """Returning False would produce DRF's own 403 and lose the upgrade payload."""
    from common.workspaces import active_workspace

    request = APIRequestFactory().get("/playbook/")
    force_authenticate(request, user=user)
    drf_request = APIView().initialize_request(request)
    drf_request.user = user

    with pytest.raises(FeatureNotAvailable):
        HasFeature("playbook")().has_permission(drf_request, PlaybookView())

    assert active_workspace(drf_request) == workspace


def test_unauthenticated_client_gets_the_envelope(workspace) -> None:
    assert APIClient().get("/api/v1/billing/entitlements/").json()["error"]["code"] == (
        "not_authenticated"
    )
