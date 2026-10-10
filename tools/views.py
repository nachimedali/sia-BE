"""Tool endpoints (design.md §8.10, C-11 / P0-04).

`POST /tools/{slug}/` is **the scoped exception** to design.md §11's rule that
a provider call never happens inside a request. It is scoped deliberately: one
credit, no product, no voice profile, no publishing account, and an answer the
user is sitting and waiting for. A job id to poll for a headline is worse
product than a two-second wait, and that is the entire justification — it does
not generalise to anything on `ai_q`.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.workspaces import authenticated_user, request_workspace
from tools import readiness as readiness_service
from tools import services
from tools.models import ToolConfig
from tools.serializers import (
    ToolConfigSerializer,
    ToolReadinessSerializer,
    ToolRunSerializer,
    ToolUsageSerializer,
)


class ToolListView(APIView):
    """Reference data, so no workspace scoping and no pagination: six rows
    that are the same for everybody."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: ToolConfigSerializer(many=True)}, summary="The available quick tools"
    )
    def get(self, request: Request) -> Response:
        return Response(ToolConfigSerializer(ToolConfig.objects.all(), many=True).data)


class ToolReadinessView(APIView):
    """Which tools can run for *this* workspace, and why the rest cannot.

    Separate from `ToolListView` rather than folded into it: that endpoint is
    reference data — six rows, identical for everybody, cacheable — and adding
    a workspace-dependent field to it would quietly make it neither.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: ToolReadinessSerializer},
        summary="Tool setup state, and each tool's own blockers",
    )
    def get(self, request: Request) -> Response:
        payload = readiness_service.tools_readiness(request_workspace(request))
        return Response(ToolReadinessSerializer(payload).data)


class ToolRunView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ToolRunSerializer,
        responses={201: ToolUsageSerializer},
        summary="Run a tool and debit one credit",
    )
    def post(self, request: Request, slug: str) -> Response:
        payload = ToolRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        usage = services.run(
            workspace=request_workspace(request),
            user=authenticated_user(request),
            slug=slug,
            payload={k: v for k, v in payload.validated_data.items() if v},
        )
        return Response(ToolUsageSerializer(usage).data, status=status.HTTP_201_CREATED)
