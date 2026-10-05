"""Video endpoints (steps-plan S3). Views parse and serialise; `ai.services.video`
decides. Every lookup is scoped to the request's workspace, so another tenant's
render, still or post is a 404, never a 403 (rule 3)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import VideoRender, VideoReview
from ai.services import video
from ai.video_serializers import (
    VideoCatalogueSerializer,
    VideoEstimateSerializer,
    VideoRenderCreateSerializer,
    VideoRenderListSerializer,
    VideoRenderSerializer,
    VideoRequestSerializer,
    VideoReviewSerializer,
    VideoSendResponseSerializer,
    VideoSendSerializer,
)
from billing.models import UNLIMITED
from billing.permissions import HasFlag
from billing.services.entitlements import entitlements_for
from billing.services.flags import VIDEO_S3
from common.workspaces import authenticated_user, request_workspace
from content.models import MediaAsset, Post
from content.services.revisions import UNEDITABLE_STATUSES
from workspaces.models import Permission, Workspace
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasFlag(VIDEO_S3), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(VIDEO_S3), HasPermission(Permission.EDIT)]
#: How many renders the queue shows. The newest; older ones are on their posts.
LIST_LIMIT = 30


def _render(request: Request, pk: int) -> VideoRender:
    render = (
        VideoRender.objects.select_related("source", "output")
        .filter(workspace=request_workspace(request), pk=pk)
        .first()
    )
    if render is None:
        raise NotFound
    return render


def _context(workspace: Workspace, renders: list[VideoRender]) -> dict[str, Any]:
    """Every still the renders were made from, in one query."""
    ids: set[int] = set()
    for render in renders:
        if render.spec.get("source") is not None:
            ids.add(render.spec["source"])
        ids.update(shot["media"] for shot in render.spec.get("shots", []))
    assets = {
        asset.pk: asset for asset in MediaAsset.objects.filter(workspace=workspace, pk__in=ids)
    }
    return {"assets": assets, "positions": video.queue_positions(workspace)}


def _serialise(workspace: Workspace, render: VideoRender) -> dict[str, Any]:
    return dict(VideoRenderSerializer(render, context=_context(workspace, [render])).data)


class VideoCatalogueView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(
        responses={200: VideoCatalogueSerializer},
        summary="The Motion step's choices and prices",
        description=(
            "Lengths, motions, frames, reel styles, music beds and surcharges, as rows; every "
            "price is `metadata.credits`. 404 with the `video_s3` flag off."
        ),
    )
    def get(self, request: Request) -> Response:
        rows = video.catalogue()
        return Response(
            VideoCatalogueSerializer(
                {
                    "lengths": rows["video_length"],
                    "motions": rows["motion"],
                    "aspects": rows["video_aspect"],
                    "reel_styles": rows["reel_style"],
                    "music": rows["music"],
                    "extras": rows["video_extra"],
                    "version": video.catalogue_version(rows),
                }
            ).data
        )


class VideoEstimateView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=VideoRequestSerializer,
        responses={200: VideoEstimateSerializer},
        summary="Price a clip or a reel",
        description=(
            "Nothing is held or charged. 402 `feature_not_available` when the plan has no "
            "video; 400 for a key not in the catalogue or a shot count the length refuses; "
            "404 for a still or product not in this workspace; 422 `video_unavailable` when "
            "no video vendor is configured."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = VideoRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        quote = video.estimate(request_workspace(request), dict(payload.validated_data))
        return Response(quote.as_dict())


class VideoRenderListView(APIView):
    def get_permissions(self) -> list[Any]:
        return [p() for p in (_WRITE if self.request.method == "POST" else _READ)]

    @extend_schema(
        responses={200: VideoRenderListSerializer},
        summary="The render queue",
        description="The newest renders (hidden ones left out), with the credit balance and "
        "what is held by renders in flight.",
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        renders = list(
            VideoRender.objects.filter(workspace=workspace)
            .exclude(review=VideoReview.HIDDEN)
            .select_related("source", "output")[:LIST_LIMIT]
        )
        remaining = entitlements_for(workspace).credits_remaining()
        return Response(
            {
                "renders": VideoRenderSerializer(
                    renders, many=True, context=_context(workspace, renders)
                ).data,
                "credits": {
                    "balance": None if remaining == UNLIMITED else remaining,
                    "held": video.held_credits(workspace),
                },
            }
        )

    @extend_schema(
        request=VideoRenderCreateSerializer,
        responses={202: VideoRenderSerializer},
        summary="Confirm an estimate and queue the render",
        description=(
            "Holds the price and queues the render; poll `GET /ai/video/renders/{id}/`. "
            "Credits are taken only if the render passes the quality gate. 409 "
            "`estimate_changed` (with the new estimate) when the price moved since it was "
            "confirmed; 402 `insufficient_credits` when the balance less what is held cannot "
            "cover it; 409 `render_not_reviewable` when `retry_of` is not a failed render."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = VideoRenderCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        retry_of = None
        if data.get("retry_of") is not None:
            retry_of = VideoRender.objects.filter(workspace=workspace, pk=data["retry_of"]).first()
            if retry_of is None:
                raise NotFound
        post = None
        if data.get("post") is not None:
            post = Post.objects.filter(workspace=workspace, pk=data["post"]).first()
            if post is None:
                raise NotFound
            if post.status in UNEDITABLE_STATUSES:
                raise video.RenderNotReviewableError(
                    "This post can no longer change.", detail={"post": post.pk}
                )
        render = video.start_render(
            workspace,
            user=authenticated_user(request),
            data=data,
            estimate_id=data["estimate_id"],
            retry_of=retry_of,
            post=post,
        )
        return Response(_serialise(workspace, render), status=status.HTTP_202_ACCEPTED)


class VideoRenderDetailView(APIView):
    def get_permissions(self) -> list[Any]:
        return [p() for p in (_WRITE if self.request.method == "PATCH" else _READ)]

    @extend_schema(responses={200: VideoRenderSerializer}, summary="One render")
    def get(self, request: Request, pk: int) -> Response:
        return Response(_serialise(request_workspace(request), _render(request, pk)))

    @extend_schema(
        request=VideoReviewSerializer,
        responses={200: VideoRenderSerializer},
        summary="Accept, discard or hide a render",
        description="Blank `review` undoes. 409 `render_not_reviewable` while it renders, "
        "or once it is on a post.",
    )
    def patch(self, request: Request, pk: int) -> Response:
        render = _render(request, pk)
        payload = VideoReviewSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        video.review(render, payload.validated_data["review"])
        return Response(_serialise(request_workspace(request), render))


class VideoRenderSendView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=VideoSendSerializer,
        responses={201: VideoSendResponseSerializer},
        summary="Send accepted renders to the calendar as draft posts",
        description=(
            "One draft post per render, the clip as its media and the post text as its "
            "caption. Scheduling and approval are the post's own path; nothing publishes "
            "from here. 409 `render_not_reviewable` for a render not accepted or already sent."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = VideoSendSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = list(dict.fromkeys(payload.validated_data["renders"]))
        renders = list(
            VideoRender.objects.filter(workspace=workspace, pk__in=ids).select_related(
                "output", "product"
            )
        )
        if len(renders) != len(ids):
            raise NotFound
        renders.sort(key=lambda render: ids.index(render.pk))
        posts = video.send(
            workspace,
            user=authenticated_user(request),
            renders=renders,
            platforms=payload.validated_data["platforms"],
        )
        return Response({"posts": [post.pk for post in posts]}, status=status.HTTP_201_CREATED)
