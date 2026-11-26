"""S21 — Studio projects (S21-01) and layers (S21-02). Every lookup is scoped
to the request's workspace, so another tenant's project is a 404 (rule 3)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.db.models.fields.json import KeyTransform
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai import video_views
from ai.models import StudioProject
from ai.services import layers
from ai.studio_serializers import (
    StillLayersSerializer,
    StillSendResponseSerializer,
    StillSendSerializer,
    StudioProjectCardSerializer,
    StudioProjectSerializer,
    VideoLayersSerializer,
)
from ai.video_serializers import VideoRenderSerializer
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.models import MediaAsset, MediaKind
from content.serializers import MediaAssetSerializer
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasPermission(Permission.EDIT)]


@extend_schema_view(
    list=extend_schema(
        responses={200: StudioProjectCardSerializer(many=True)},
        summary="The workspace's Studio projects, newest first",
    ),
)
class StudioProjectViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[StudioProject]):
    queryset = StudioProject.objects.all()
    pagination_class = DefaultPagination
    http_method_names: list[str] = ["get", "post", "patch", "delete", "head", "options"]  # noqa: RUF012

    def get_permissions(self) -> list[Any]:
        return [
            p() for p in (_READ if self.request.method in ("GET", "HEAD", "OPTIONS") else _WRITE)
        ]

    def get_serializer_class(self) -> Any:
        return StudioProjectCardSerializer if self.action == "list" else StudioProjectSerializer

    def get_queryset(self) -> QuerySet[StudioProject]:
        rows = super().get_queryset().select_related("created_by")
        # The cards read only the page's summary: the rest of the state (up to
        # 256 KB a project) stays in the database.
        if self.action == "list":
            rows = rows.defer("state").annotate(summary=KeyTransform("summary", "state"))
        return rows

    def perform_create(self, serializer: Any) -> None:
        serializer.save(
            workspace=request_workspace(self.request), created_by=authenticated_user(self.request)
        )


class VideoRenderLayersView(APIView):
    """S21-02: the Studio's layers over a finished clip, free."""

    permission_classes: list[Any] = video_views._WRITE

    @extend_schema(
        request=VideoLayersSerializer,
        responses={202: VideoRenderSerializer},
        summary="Draw the Studio's layers over a finished render",
        description=(
            "Free: no hold and no debit. Drawn on `media_q` from the plain clip; `layers_status` "
            "is `running` until it is done. An empty list gives the plain clip back. 400 "
            "`layers_invalid` (detail: index, field), 409 `render_not_ready`, `render_sent`, "
            "`layers_running`."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        render = video_views._render(request, pk)
        payload = VideoLayersSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        render = layers.request_video_layers(
            render, payload.validated_data["layers"], payload.validated_data["kit"]
        )
        return Response(
            video_views._serialise(request_workspace(request), render),
            status=status.HTTP_202_ACCEPTED,
        )


class StillLayersView(APIView):
    """S21-02: a still at its aspect with the Studio's layers on it, free."""

    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=StillLayersSerializer,
        responses={201: MediaAssetSerializer},
        summary="Draw the Studio's layers onto a still",
        description=(
            "A new picture derived from `media`, cropped to `aspect` (`9:16` or `4:5`). "
            "Free. 400 `layers_invalid` (detail: index, field)."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = StillLayersSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        media = MediaAsset.objects.filter(
            workspace=workspace, pk=payload.validated_data["media"], kind=MediaKind.IMAGE
        ).first()
        if media is None:
            raise NotFound
        made = layers.layer_still(
            workspace=workspace,
            media=media,
            aspect=payload.validated_data["aspect"],
            items=payload.validated_data["layers"],
            kit=payload.validated_data["kit"],
        )
        return Response(
            MediaAssetSerializer(made, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class StillSendView(APIView):
    """S21: a finished still to the drafts, as the Studio's own work."""

    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=StillSendSerializer,
        responses={201: StillSendResponseSerializer},
        summary="Send a finished Studio still to the drafts",
        description=(
            "One draft post with the still as its media and `text` as its caption, marked "
            "as the Studio's work; the AI-generated label is added when the picture shows a "
            "drawn person. Approval is the post's own path; nothing publishes from here."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = StillSendSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        media = MediaAsset.objects.filter(
            workspace=workspace, pk=payload.validated_data["media"], kind=MediaKind.IMAGE
        ).first()
        if media is None:
            raise NotFound
        post = layers.send_still(
            workspace=workspace,
            user=authenticated_user(request),
            media=media,
            text=payload.validated_data["text"],
            language=payload.validated_data["language"],
            platforms=payload.validated_data["platforms"],
        )
        return Response({"post": post.pk}, status=status.HTTP_201_CREATED)
