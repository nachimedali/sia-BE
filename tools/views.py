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
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import NotFoundError
from common.workspaces import authenticated_user, request_workspace
from tools import readiness as readiness_service
from tools import services
from tools.models import ToolConfig
from tools.serializers import (
    ToolConfigSerializer,
    ToolPriceSerializer,
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
        payload["can_edit_prices"] = bool(request.user.is_staff)
        return Response(ToolReadinessSerializer(payload).data)


class ToolPriceView(APIView):
    """`PATCH /tools/{slug}/price/` — platform staff only (X-38).

    `ToolConfig` is one row per tool for every customer, so this is an
    operator's control, not an organization admin's: a customer repricing it
    would reprice everyone. Applies from the next run; past usage keeps what
    it was charged.
    """

    permission_classes = [IsAdminUser]

    @extend_schema(
        request=ToolPriceSerializer,
        responses={200: ToolConfigSerializer},
        summary="Change a tool's price per run (staff only)",
    )
    def patch(self, request: Request, slug: str) -> Response:
        config = ToolConfig.objects.filter(slug=slug).first()
        if config is None:
            raise NotFoundError("No such tool.", code="tool_not_found")
        payload = ToolPriceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        config.credits_cost = payload.validated_data["credits_cost"]
        config.save(update_fields=["credits_cost", "updated_at"])
        return Response(ToolConfigSerializer(config).data)


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
