"""Benchmark surfaces (Phase 8).

Every view answers for the caller's own workspace — `request_workspace`
resolves it, so another tenant's id is a 404 on every route. Reading needs
`analyze`, the same as the rest of analytics; granting or revoking consent needs
`admin`, because it decides what leaves the workspace.

Order of refusal is deliberate: flag off is a 404 before any permission is
consulted, since before this phase shipped the route did not exist at all.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from benchmarks.serializers import (
    BenchmarkParticipationSerializer,
    BenchmarksSerializer,
    GrantConsentSerializer,
)
from benchmarks.services import consent, reading
from billing.permissions import HasFlag
from billing.services.flags import COHORT_V8
from common.workspaces import authenticated_user, request_workspace
from workspaces.models import Permission
from workspaces.permissions import HasPermission


class BenchmarkListView(APIView):
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(COHORT_V8),
        HasPermission(Permission.ANALYZE),
    ]

    @extend_schema(
        responses={200: BenchmarksSerializer},
        summary="How this workspace's cohorts are performing",
        description=(
            "One entry per cohort this workspace's own posts belong to. A cohort "
            "below the thresholds is `insufficient_cohort_data` with its shortfall "
            "and no statistic. 409 `benchmark_consent_required` for a workspace "
            "that does not contribute."
        ),
    )
    def get(self, request: Request) -> Response:
        payload = reading.benchmarks_for(request_workspace(request))
        return Response(BenchmarksSerializer(payload).data)


class BenchmarkParticipationView(APIView):
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(COHORT_V8),
        HasPermission(Permission.ANALYZE),
    ]

    @extend_schema(
        responses={200: BenchmarkParticipationSerializer},
        summary="Whether this workspace contributes, and under which terms",
    )
    def get(self, request: Request) -> Response:
        payload = reading.participation(request_workspace(request))
        return Response(BenchmarkParticipationSerializer(payload).data)


class BenchmarkGrantView(APIView):
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(COHORT_V8),
        HasPermission(Permission.ADMIN),
    ]

    @extend_schema(
        request=GrantConsentSerializer,
        responses={200: BenchmarkParticipationSerializer},
        summary="Opt into cohort benchmarks under the current terms",
    )
    def post(self, request: Request) -> Response:
        body = GrantConsentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        workspace = request_workspace(request)
        consent.grant(
            workspace,
            actor=authenticated_user(request),
            policy_version=body.validated_data["policy_version"],
            market=body.validated_data["market"],
        )
        workspace.refresh_from_db()
        return Response(BenchmarkParticipationSerializer(reading.participation(workspace)).data)


class BenchmarkRevokeView(APIView):
    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(COHORT_V8),
        HasPermission(Permission.ADMIN),
    ]

    @extend_schema(
        request=None,
        responses={200: BenchmarkParticipationSerializer},
        summary="Stop contributing — published benchmarks are not recomputed",
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        consent.revoke(workspace, actor=authenticated_user(request))
        return Response(BenchmarkParticipationSerializer(reading.participation(workspace)).data)
