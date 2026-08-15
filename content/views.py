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
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from billing.permissions import HasFeature
from common.exceptions import OCCSError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace, authenticated_user
from content.models import MediaAsset, Platform, Post, PostMediaAttachment, PostStatus
from content.serializers import (
    MediaAssetSerializer,
    MediaAssetUploadSerializer,
    PostPreviewRequestSerializer,
    PostPreviewResponseSerializer,
    PostScheduleRequestSerializer,
    PostSerializer,
)
from content.services.adaptation import render_payloads
from content.services.media import ingest_media
from content.services.posts import create_post, update_post
from scheduling.services import schedule_post
from workspaces.models import PostComment, Role
from workspaces.permissions import HasRole, caller_role, role_at_least
from workspaces.serializers import (
    ApprovalActionSerializer,
    ApprovalNoteRequestSerializer,
    PostCommentSerializer,
)
from workspaces.services import approvals

#: design.md §8.8: submit/approve/request-changes/reject/comments are all
#: part of "the approval workflow" — Advanced only (`test_approval_workflow_
#: gated_to_advanced`).
APPROVAL_FEATURE = "approval_workflow"

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
    queryset = Post.objects.select_related("category", "origin_post", "author").prefetch_related(
        _ORDERED_MEDIA_ATTACHMENTS
    )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "status",
                str,
                description="Repeatable. Restricts the list to these statuses; an unknown "
                "value is a 400 rather than a silently empty page.",
                many=True,
                enum=PostStatus.values,
            )
        ]
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> Any:
        """`?status=` exists for the review queue, which is a *subset* of the
        workspace's posts and has to be the whole subset: `max_page_size` is
        100, so filtering a page client-side would quietly drop the 101st post
        awaiting review. An unrecognised value is rejected rather than ignored,
        because a typo that returns everything is worse than one that 400s.
        """
        queryset = super().get_queryset()
        statuses = self.request.query_params.getlist("status")
        if not statuses:
            return queryset

        unknown = sorted(set(statuses) - set(PostStatus.values))
        if unknown:
            raise OCCSError(
                f"Unknown post status: {', '.join(unknown)}.",
                code="invalid_status",
                detail={"status": unknown},
            )
        return queryset.filter(status__in=statuses)

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

    @extend_schema(
        request=PostScheduleRequestSerializer,
        responses={200: PostSerializer},
        summary="Schedule a post for reminder or auto-publish delivery",
        description=(
            "Validates `scheduled_at` against the workspace's "
            "`Plan.scheduling_horizon_days` (402 past the horizon, D13/I8) "
            "and, for `delivery_mode=REMINDER`, arms the Reminder that Beat "
            "sends on time (implementation.md Phase 8)."
        ),
    )
    @action(detail=True, methods=["post"])
    def schedule(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        payload = PostScheduleRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        post = schedule_post(
            post=post, delivery_mode=data["delivery_mode"], scheduled_at=data["scheduled_at"]
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=None,
        responses={200: PostSerializer},
        summary="Submit a draft for review",
        description="DRAFT or CHANGES_REQUESTED → PENDING_REVIEW. Any role but VIEWER "
        "(design.md §8.8); an illegal transition is 409, not a silent no-op.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            IsAuthenticated,
            HasFeature(APPROVAL_FEATURE),
            HasRole(Role.CONTRIBUTOR),
        ],
    )
    def submit(self, request: Request, pk: str | None = None) -> Response:
        post = approvals.submit_for_review(self.get_object(), actor=authenticated_user(request))
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Approve a post under review",
        description="PENDING_REVIEW → APPROVED. ADMIN+ only.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasFeature(APPROVAL_FEATURE), HasRole(Role.ADMIN)],
    )
    def approve(self, request: Request, pk: str | None = None) -> Response:
        note = self._approval_note(request)
        post = approvals.approve(self.get_object(), actor=authenticated_user(request), note=note)
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Send a post back with changes requested",
        description="PENDING_REVIEW → CHANGES_REQUESTED. ADMIN+ only; `note` is required "
        "— it is the only thing the author has to act on.",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="request-changes",
        permission_classes=[IsAuthenticated, HasFeature(APPROVAL_FEATURE), HasRole(Role.ADMIN)],
    )
    def request_changes(self, request: Request, pk: str | None = None) -> Response:
        note = self._approval_note(request)
        if not note:
            raise OCCSError("A note is required when requesting changes.", code="note_required")
        post = approvals.request_changes(
            self.get_object(), actor=authenticated_user(request), note=note
        )
        return Response(PostSerializer(post, context={"request": request}).data)

    @extend_schema(
        request=ApprovalNoteRequestSerializer,
        responses={200: PostSerializer},
        summary="Reject a post under review",
        description="PENDING_REVIEW → REJECTED. ADMIN+ only.",
    )
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, HasFeature(APPROVAL_FEATURE), HasRole(Role.ADMIN)],
    )
    def reject(self, request: Request, pk: str | None = None) -> Response:
        note = self._approval_note(request)
        post = approvals.reject(self.get_object(), actor=authenticated_user(request), note=note)
        return Response(PostSerializer(post, context={"request": request}).data)

    @staticmethod
    def _approval_note(request: Request) -> str:
        payload = ApprovalNoteRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        note: str = payload.validated_data["note"]
        return note

    @extend_schema(
        responses={200: ApprovalActionSerializer(many=True)},
        summary="This post's approval history, oldest first",
        description="The append-only `ApprovalAction` rows behind the post's current status. "
        "Scoped to one post rather than read from `GET /workspaces/audit-log/`, which is "
        "workspace-wide and paginated — a reviewer wants this post's trail, not a page of "
        "everyone's.",
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="approvals",
        # Not named `approvals`: this module imports the `approvals` *service*,
        # and a method of that name reads as a shadow of it to anyone skimming
        # the class, even though Python resolves the two in different scopes.
        permission_classes=[IsAuthenticated, HasFeature(APPROVAL_FEATURE)],
    )
    def approval_history(self, request: Request, pk: str | None = None) -> Response:
        trail = self.get_object().approval_actions.select_related("actor").order_by("created_at")
        return Response(ApprovalActionSerializer(trail, many=True).data)

    @extend_schema(
        request=PostCommentSerializer,
        responses={200: PostCommentSerializer(many=True), 201: PostCommentSerializer},
        summary="Read or add comments on this post",
        description="GET is open to any workspace member; POST needs any role but VIEWER, "
        "the same gate `submit` uses (design.md §8.8's collaboration surface).",
    )
    @action(
        detail=True,
        methods=["get", "post"],
        permission_classes=[IsAuthenticated, HasFeature(APPROVAL_FEATURE)],
    )
    def comments(self, request: Request, pk: str | None = None) -> Response:
        post = self.get_object()
        if request.method == "POST":
            if not role_at_least(caller_role(request), Role.CONTRIBUTOR):
                raise PermissionDenied("VIEWER cannot comment.")
            payload = PostCommentSerializer(data=request.data, context={"post": post})
            payload.is_valid(raise_exception=True)
            comment = approvals.add_comment(
                post,
                author=authenticated_user(request),
                body=payload.validated_data["body"],
                parent=payload.validated_data.get("parent"),
            )
            return Response(PostCommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        thread = post.comments.select_related("author").order_by("created_at")
        return Response(PostCommentSerializer(thread, many=True).data)

    @extend_schema(
        request=None,
        responses={200: PostCommentSerializer},
        summary="Mark a comment resolved",
        parameters=[OpenApiParameter("comment_pk", int, OpenApiParameter.PATH)],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"comments/(?P<comment_pk>[^/.]+)/resolve",
        permission_classes=[
            IsAuthenticated,
            HasFeature(APPROVAL_FEATURE),
            HasRole(Role.CONTRIBUTOR),
        ],
    )
    def resolve_comment(
        self, request: Request, pk: str | None = None, comment_pk: str | None = None
    ) -> Response:
        post = self.get_object()
        comment = get_object_or_404(PostComment, pk=comment_pk, post=post)
        comment = approvals.resolve_comment(comment)
        return Response(PostCommentSerializer(comment).data)


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
