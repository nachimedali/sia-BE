"""Trend endpoints (design.md §7, D11, D18).

Two plain paths, not a router registration. A `TrendCluster` belongs to a
`Category`, not to a workspace — every workspace in ceramics reads the same
corpus, which is the whole economic argument for extracting on demand and
caching (D11). There is therefore no workspace-scoped object here for the
tenancy sweep to walk (A52), and the category comes from the caller's own
workspace rather than from a pk they could change.

Free is **not** refused. §4.1 gives Free "teaser only" and §10.5 says a 402
reaching the user as an error is a UI bug — so the gate here shapes the
response (fewer clusters, no exemplars, `locked: true`) instead of raising.
That is the difference between this and `channels`, where Free is stopped at
the door because connecting an account is per-seat COGS: reading a corpus that
has already been extracted for someone else costs nothing.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch, prefetch_related_objects
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFeature
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from common.workspaces import active_workspace
from content.models import Platform
from trends import services
from trends.models import TrendItem
from trends.serializers import (
    TrendClusterSerializer,
    TrendRefreshRequestSerializer,
    TrendTeaserSerializer,
)
from workspaces.models import Workspace

FEATURE_KEY = "trend_engine"


def _resolve_platform(value: str | None, workspace: Workspace) -> str:
    """The platform to report on: whatever was asked for, or the workspace's
    own first intended platform, which onboarding already collected."""
    if value:
        if value not in Platform.values:
            raise OCCSError(
                "That platform is not supported.",
                code="unsupported_platform",
                detail={"platform": value},
            )
        return value
    return str(workspace.platforms[0]) if workspace.platforms else Platform.INSTAGRAM


def _category_id(workspace: Workspace) -> int:
    if workspace.category_id is None:
        raise OCCSError(
            "Set your category in onboarding before reading trends.",
            code="category_required",
        )
    return int(workspace.category_id)


def _render(clusters: list[Any], *, unlocked: bool) -> Response:
    if unlocked and clusters:
        # One query for every cluster's exemplars instead of one per cluster —
        # `prefetch_related_objects` works on an already-fetched list, so this
        # applies regardless of whether `clusters` came from the cache or was
        # just built by a fresh extraction. `.only()` skips each item's
        # 1536-float embedding, which `TrendExemplarSerializer` never reads.
        prefetch_related_objects(
            clusters,
            Prefetch(
                "items",
                queryset=TrendItem.objects.only(
                    "id",
                    "cluster_id",
                    "modality",
                    "author_handle",
                    "body",
                    "media_url",
                    "posted_at",
                    "composite_score",
                ),
            ),
        )
    serializer_class = TrendClusterSerializer if unlocked else TrendTeaserSerializer
    return Response(serializer_class(clusters, many=True).data)


class TrendListView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("platform", str, description="Defaults to the workspace's first.")
        ],
        responses={200: TrendClusterSerializer(many=True)},
        summary="Ranked trend clusters for this workspace's category",
        description=(
            "Served from the cached corpus (D11, 30-day TTL) and extracted on "
            "first ask. Free plans receive a capped teaser with `locked: true` "
            "and no exemplars."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = active_workspace(request)
        platform = _resolve_platform(request.query_params.get("platform"), workspace)
        unlocked = bool(entitlements_for(workspace).feature(FEATURE_KEY))

        clusters = services.extract(category_id=_category_id(workspace), platform=platform)
        return _render(clusters, unlocked=unlocked)


class TrendRefreshView(APIView):
    """The manual refresh D11 asks for.

    Gated properly — a 402, not a teaser — because unlike reading, extracting
    spends vendor quota and provider budget on this caller's behalf. Free reads
    what someone else's refresh produced.
    """

    permission_classes: list[Any] = [IsAuthenticated, HasFeature(FEATURE_KEY)]

    @extend_schema(
        request=TrendRefreshRequestSerializer,
        responses={200: TrendClusterSerializer(many=True)},
        summary="Force a re-extraction for this workspace's category",
    )
    def post(self, request: Request) -> Response:
        workspace = active_workspace(request)

        payload = TrendRefreshRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        platform = _resolve_platform(payload.validated_data["platform"], workspace)

        clusters = services.extract(
            category_id=_category_id(workspace), platform=platform, force=True
        )
        return _render(clusters, unlocked=True)
