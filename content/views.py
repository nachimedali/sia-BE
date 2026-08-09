"""Content endpoints (design.md §7).

Views parse and serialise; `content.services` decides (implementation.md §4.1).
`PostViewSet` and `MediaAssetViewSet` are the first two entries on the router
in `config/api_urls.py`, which is what the Phase 4 tenancy sweep
(`test_cross_workspace_access_returns_404_on_every_viewset`) walks (design.md
A52).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace, authenticated_user
from content.models import MediaAsset, Platform, Post, PostMediaAttachment
from content.serializers import (
    MediaAssetSerializer,
    MediaAssetUploadSerializer,
    PostPreviewRequestSerializer,
    PostPreviewResponseSerializer,
    PostSerializer,
)
from content.services.adaptation import render_payloads
from content.services.media import ingest_media
from content.services.posts import create_post, update_post

# `Post.ordered_media()` reads `media_attachments`, not the `media_assets` M2M
# manager directly (content/models.py) — the Prefetch has to target the same
# accessor, or it fetches rows nothing ever reads and every post in a list
# response re-queries its media anyway.
_ORDERED_MEDIA_ATTACHMENTS = Prefetch(
    "media_attachments",
    queryset=PostMediaAttachment.objects.select_related("media_asset").order_by("order"),
)


class PostViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[Post]):
    serializer_class = PostSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = Post.objects.select_related("category", "origin_post").prefetch_related(
        _ORDERED_MEDIA_ATTACHMENTS
    )

    def perform_create(self, serializer: BaseSerializer[Post]) -> None:
        assert isinstance(serializer, PostSerializer)  # always this view's own serializer_class
        data = serializer.validated_data
        serializer.instance = create_post(
            workspace=active_workspace(self.request),
            author=authenticated_user(self.request),
            master_body=data.get("master_body", ""),
            category=data.get("category"),
            media_assets=data.get("media_asset_ids", []),
        )

    def perform_update(self, serializer: BaseSerializer[Post]) -> None:
        assert isinstance(serializer, PostSerializer)  # always this view's own serializer_class
        assert serializer.instance is not None  # set by UpdateModelMixin.get_object() beforehand
        serializer.instance = update_post(serializer.instance, **serializer.validated_data)

    @extend_schema(
        request=PostPreviewRequestSerializer,
        responses={200: PostPreviewResponseSerializer},
        summary="Preview per-platform adaptation",
        description=(
            "Adapts a master body and media into what each requested platform "
            "would receive, without persisting anything. The publish path "
            "(Phase 9) calls the same Adaptation Engine function on the saved "
            "post, so this is provably what would be sent (design.md §8.6)."
        ),
    )
    @action(detail=False, methods=["post"])
    def preview(self, request: Request) -> Response:
        payload = PostPreviewRequestSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        workspace = active_workspace(request)
        platforms = data.get("platforms") or [
            p for p in workspace.platforms if p in Platform.values
        ]
        if not platforms:
            raise OCCSError(
                "No target platforms to preview. Pick platforms in onboarding, "
                "or pass `platforms` explicitly.",
                code="no_target_platforms",
            )

        payloads = render_payloads(
            master_body=data.get("master_body", ""),
            media_assets=list(data.get("media_asset_ids", [])),
            platforms=platforms,
        )
        return Response({"payloads": {p: payload.as_dict() for p, payload in payloads.items()}})


class MediaAssetViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet[MediaAsset],
):
    """Upload, list, retrieve, delete. No update: a media asset's bytes and
    the metadata sniffed from them are immutable once ingested — a changed
    file is a new upload, not an edit."""

    serializer_class = MediaAssetSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = MediaAsset.objects.all()

    @extend_schema(request=MediaAssetUploadSerializer, responses={201: MediaAssetSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise OCCSError("No file was uploaded.", code="missing_file")

        asset = ingest_media(workspace=active_workspace(request), upload=upload)
        serializer = self.get_serializer(asset)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
