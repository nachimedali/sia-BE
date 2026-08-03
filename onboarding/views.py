"""Onboarding endpoints (design.md §7)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from common.exceptions import OCCSError
from onboarding.serializers import (
    REQUIRED_TO_COMPLETE,
    OnboardingSerializer,
)
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
    """The workspace this request acts on.

    Phase 2 has exactly one workspace per user, so the owned workspace is
    unambiguous. Multi-workspace switching arrives with invitations in a later
    phase; this is the single place that will need to change.
    """
    workspace = (
        Workspace.objects.filter(memberships__user=authenticated_user(request))
        .select_related("plan", "category")
        .order_by("created_at")
        .first()
    )
    if workspace is None:
        raise OCCSError("This account has no workspace.", code="no_workspace")
    return workspace


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
        workspace = active_workspace(request)

        if not authenticated_user(request).is_email_verified:
            raise OCCSError(
                "Confirm your email address before finishing setup.",
                code="email_not_verified",
            )

        missing = [
            field
            for field in REQUIRED_TO_COMPLETE
            if not OnboardingSerializer._filled(workspace, field)
        ]
        if missing:
            raise OCCSError(
                "Some required details are still missing.",
                code="onboarding_incomplete",
                detail={"missing": missing},
            )

        if not workspace.onboarding_complete:
            workspace.onboarding_complete = True
            workspace.save(update_fields=["onboarding_complete", "updated_at"])

        return Response(OnboardingSerializer(workspace, context={"request": request}).data)
