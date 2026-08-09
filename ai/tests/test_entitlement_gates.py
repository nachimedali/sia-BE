"""I5 — entitlements checked at four independent gates (design.md §8.1).

Each sub-test bypasses the other three deliberately: "the serializer would
have caught it" is not an acceptable answer for why the permission class (or
the task, or the UI) failed to. Mirrors `billing/tests/test_permissions.py`'s
framing for the same invariant, applied to generation's credits gate rather
than a static feature flag.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from ai.models import GenerationKind, GenerationMode, GenerationStatus
from ai.permissions import HasSufficientCredits
from ai.serializers import GenerateRequestSerializer
from ai.services.pipeline import create_generation, run_generation
from common.exceptions import InsufficientCredits

pytestmark = pytest.mark.django_db


def _drain(workspace: Any) -> None:
    from billing.services.entitlements import entitlements_for
    from billing.services.ledger import credit_balance, debit_credits

    balance = credit_balance(workspace)
    if balance:
        debit_credits(
            workspace, balance, quota=entitlements_for(workspace).quota("monthly_ai_credits")
        )


# -----------------------------------------------------------------------------
# Gate 1 — the serializer, called directly (no permission class, no view)
# -----------------------------------------------------------------------------
def test_serializer_blocks_insufficient_credits_on_its_own(
    workspace: Any, user: Any, generation_costs: Any
) -> None:
    _drain(workspace)
    request = APIRequestFactory().post("/api/v1/ai/generate/")
    force_authenticate(request, user=user)
    drf_request = APIView().initialize_request(request)
    drf_request.user = user

    serializer = GenerateRequestSerializer(
        data={"kind": GenerationKind.TEXT, "mode": GenerationMode.IDEA, "prompt": "hi"},
        context={"request": drf_request},
    )

    with pytest.raises(InsufficientCredits):
        serializer.is_valid(raise_exception=True)


# -----------------------------------------------------------------------------
# Gate 2 — the DRF permission, called directly (no serializer, no view)
# -----------------------------------------------------------------------------
def test_permission_blocks_insufficient_credits_on_its_own(
    workspace: Any, user: Any, generation_costs: Any
) -> None:
    _drain(workspace)
    request = APIRequestFactory().post(
        "/api/v1/ai/generate/",
        {"kind": GenerationKind.TEXT, "mode": GenerationMode.IDEA},
        format="json",
    )
    force_authenticate(request, user=user)
    drf_request: Request = APIView().initialize_request(request)
    drf_request.user = user

    with pytest.raises(InsufficientCredits):
        HasSufficientCredits().has_permission(drf_request, APIView())


# -----------------------------------------------------------------------------
# Gate 3 — the Celery task's own preflight, i.e. the debit inside
# `run_generation` — called directly, well past the serializer and permission
# -----------------------------------------------------------------------------
def test_task_preflight_blocks_insufficient_credits_on_its_own(
    workspace: Any, user: Any, generation_costs: Any
) -> None:
    # Sufficient credits at creation time (the earlier gates would pass) —
    # the point is that spending happens between them, so *this* gate is the
    # one standing between a stale preflight and an overspend.
    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="hi",
    )
    _drain(workspace)

    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.FAILED


# -----------------------------------------------------------------------------
# Gate 4 — the UI's own read, GET /billing/entitlements/
# -----------------------------------------------------------------------------
def test_ui_entitlements_reports_the_shortfall_on_its_own(
    auth_client: Any, workspace: Any, generation_costs: Any
) -> None:
    _drain(workspace)

    response = auth_client.get("/api/v1/billing/entitlements/")

    assert response.status_code == 200
    assert response.json()["credits_remaining"] == 0
