"""`GET /search/` — the workspace search behind the top bar's `/app/search`."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.visibility import request_audience
from common.workspaces import request_workspace
from search import services
from search.serializers import SearchResponseSerializer
from workspaces.models import Permission
from workspaces.permissions import HasPermission


class SearchView(APIView):
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]

    @extend_schema(
        summary="Search this workspace's posts, products and campaigns",
        description="Every word must match some field. Posts and campaigns are searched "
        "inside the plan's `analytics_history_days`; older matches are only counted, in "
        "`history.older_matches`. 400 `invalid_query` (under 2 or over 120 characters), "
        "`invalid_type`, `invalid_page`.",
        parameters=[
            OpenApiParameter("q", str, required=True),
            OpenApiParameter("type", str, enum=list(services.TYPES), default="all"),
            OpenApiParameter(
                "page",
                int,
                default=1,
                description=f"Single-type tabs only. Cumulative: page N returns the first "
                f"N x {services.PAGE_SIZE} rows. At most {services.MAX_PAGE}.",
            ),
        ],
        responses={200: SearchResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        params = request.query_params
        q, kind, page = services.validate(
            params.get("q", ""), params.get("type", "all"), params.get("page", "1")
        )
        return Response(
            services.search(
                workspace=request_workspace(request),
                audience=request_audience(request),
                q=q,
                kind=kind,
                page=page,
            )
        )
