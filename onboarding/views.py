"""Onboarding endpoints (design.md §7).

Views parse and serialise; `onboarding.services.wizard` decides
(implementation.md §4.1).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.workspaces import active_workspace, authenticated_user
from onboarding.serializers import OnboardingSerializer
from onboarding.services.wizard import complete_onboarding


class OnboardingView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: OnboardingSerializer},
        summary="Read onboarding state",
        description=(
            "Returns the workspace fields the wizard collects plus `current_step` — "
            "the first step not yet satisfied, which is what makes the wizard resumable "
            "across sessions and devices."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = active_workspace(request)
        return Response(OnboardingSerializer(workspace, context={"request": request}).data)

    @extend_schema(
        request=OnboardingSerializer,
        responses={200: OnboardingSerializer},
        summary="Save partial onboarding progress",
        description="Partial by design: each step PATCHes only the fields it owns.",
    )
    def patch(self, request: Request) -> Response:
        workspace = active_workspace(request)
        serializer = OnboardingSerializer(
            workspace, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class OnboardingCompleteView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={200: OnboardingSerializer},
        summary="Finish onboarding",
        description=(
            "Validates the required fields and flips `onboarding_complete`. "
            "Requires a verified email — step 1 is a gate, not a formality."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = complete_onboarding(active_workspace(request), authenticated_user(request))
        return Response(OnboardingSerializer(workspace, context={"request": request}).data)
