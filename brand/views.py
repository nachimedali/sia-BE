"""Brand import endpoints (steps-plan S1). Views parse and serialise; the
services decide. Every lookup is scoped to the request's workspace, so another
tenant's import is a 404, never a 403 (rule 3)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFlag
from billing.services.flags import BRAND_IMPORT_S1
from brand.models import BrandImport
from brand.serializers import (
    BrandApplyResponseSerializer,
    BrandCoreSerializer,
    BrandImportReviewSerializer,
    BrandImportSerializer,
    BrandImportStartSerializer,
)
from brand.services import imports
from common.workspaces import authenticated_user, request_workspace
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasFlag(BRAND_IMPORT_S1), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(BRAND_IMPORT_S1), HasPermission(Permission.EDIT)]


def _import(request: Request, pk: int) -> BrandImport:
    run = BrandImport.objects.filter(workspace=request_workspace(request), pk=pk).first()
    if run is None:
        raise NotFound
    return run


class BrandImportListView(APIView):
    def get_permissions(self) -> list[Any]:
        return [p() for p in (_WRITE if self.request.method == "POST" else _READ)]

    @extend_schema(
        request=BrandImportStartSerializer,
        responses={201: BrandImportSerializer},
        summary="Start reading a website into a brand import",
        description=(
            "Queues the import on `ai_q`; poll `GET /brand/imports/{id}/` for the stage and "
            "progress. Reads public pages only (home, shop, about, contact, a few products) "
            "and never applies anything. 400 `invalid_website`; 409 `import_running`."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = BrandImportStartSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        run = imports.start_import(
            request_workspace(request),
            user=authenticated_user(request),
            url=payload.validated_data["url"],
        )
        return Response(BrandImportSerializer(run).data, status=status.HTTP_201_CREATED)


class BrandImportLatestView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(responses={200: BrandImportSerializer}, summary="This workspace's latest import")
    def get(self, request: Request) -> Response:
        run = BrandImport.objects.filter(workspace=request_workspace(request)).first()
        if run is None:
            raise NotFound
        return Response(BrandImportSerializer(run).data)


class BrandImportDetailView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(
        responses={200: BrandImportSerializer},
        summary="One brand import: stage, progress, result, review",
    )
    def get(self, request: Request, pk: int) -> Response:
        return Response(BrandImportSerializer(_import(request, pk)).data)


class BrandImportReviewView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=BrandImportReviewSerializer,
        responses={200: BrandImportSerializer},
        summary="Accept or edit one section of an import",
        description=(
            "409 `import_not_ready` before the reading finished; "
            "400 `invalid_section` for a bad edit."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        payload = BrandImportReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        run = imports.review(
            _import(request, pk),
            section=data["section"],
            action=data["action"],
            value=data.get("value"),
        )
        return Response(BrandImportSerializer(run).data)


class BrandImportApplyView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=None,
        responses={200: BrandApplyResponseSerializer},
        summary="Write the reviewed import into a new Brand Core version",
        description=(
            "Every section must be reviewed (409 `review_incomplete`). Sections the user edited "
            "in an earlier version are kept unless edited again (`kept`). Workspace fields are "
            "filled only where still blank (`filled`)."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        core, kept, filled = imports.apply(_import(request, pk), user=authenticated_user(request))
        return Response(
            BrandApplyResponseSerializer({"core": core, "kept": kept, "filled": filled}).data
        )


class BrandImportSkipView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=None,
        responses={200: BrandImportSerializer},
        summary="Skip the import and fill the brand in by hand",
    )
    def post(self, request: Request) -> Response:
        run = imports.skip(request_workspace(request), user=authenticated_user(request))
        return Response(BrandImportSerializer(run).data)


class BrandCoreView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(
        responses={200: BrandCoreSerializer}, summary="The workspace's current Brand Core"
    )
    def get(self, request: Request) -> Response:
        core = imports.active_core(request_workspace(request))
        if core is None:
            raise NotFound
        return Response(BrandCoreSerializer(core).data)
