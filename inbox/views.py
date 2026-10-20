"""`/inbox/…` (steps-plan S2). Every row is resolved through `queries.base`,
which starts from the workspace's own measured comments, so another tenant's
comment id is a 404 from the query and never a 403 (Part 7 rule 3)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from analytics.models import AudienceComment
from analytics.serializers import AudienceReplyResultSerializer
from analytics.services import audience
from billing.permissions import HasFlag
from billing.services.flags import INBOX_S2
from common.exceptions import NotFoundError
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from inbox.models import SavedReply
from inbox.serializers import (
    InboxCommentDetailSerializer,
    InboxCommentSerializer,
    InboxReplySerializer,
    InboxSummarySerializer,
    NoteSerializer,
    NoteWriteSerializer,
    SavedReplySerializer,
    TriageUpdateSerializer,
)
from inbox.services import coding, queries, triage
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_FILTERS = [
    OpenApiParameter("platform", str),
    OpenApiParameter("post", int),
    OpenApiParameter("sentiment", str),
    OpenApiParameter("intent", str, description="An intent, or `uncoded`."),
    OpenApiParameter("status", str, description="`open` or `done`."),
    OpenApiParameter("assignee", str, description="`me`, `none` or a user id."),
    OpenApiParameter("replied", str, description="`yes` or `no`."),
    OpenApiParameter("q", str, description="Searches the comment and its author."),
]


def _comment(request: Request, pk: int) -> AudienceComment:
    comment = queries.one(request_workspace(request), pk)
    if comment is None:
        raise NotFoundError("No such comment.", detail={"comment": pk})
    return comment


class InboxCommentListView(APIView):
    permission_classes = [IsAuthenticated, HasFlag(INBOX_S2), HasPermission(Permission.VIEW)]

    @extend_schema(
        parameters=_FILTERS,
        responses={200: InboxCommentSerializer(many=True)},
        summary="Every captured comment, newest first",
        description=(
            "Measured comments only, inside the plan's history horizon. A platform that "
            "cannot report comments is never listed as empty — see `/inbox/summary/`."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        rows = queries.apply_filters(
            queries.base(workspace), request.query_params, user=authenticated_user(request)
        )
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            InboxCommentSerializer(page, many=True, context={"request": request}).data
        )


class InboxSummaryView(APIView):
    permission_classes = [IsAuthenticated, HasFlag(INBOX_S2), HasPermission(Permission.VIEW)]

    @extend_schema(responses={200: InboxSummarySerializer}, summary="Counts and platform coverage")
    def get(self, request: Request) -> Response:
        data = queries.summary(request_workspace(request), user=authenticated_user(request))
        return Response(InboxSummarySerializer(data).data)


class InboxCommentDetailView(APIView):
    permission_classes = [IsAuthenticated, HasFlag(INBOX_S2)]

    def get_permissions(self) -> Any:
        needed = Permission.VIEW if self.request.method == "GET" else Permission.COMMENT
        return [*super().get_permissions(), HasPermission(needed)()]

    @extend_schema(responses={200: InboxCommentDetailSerializer}, summary="One comment")
    def get(self, request: Request, pk: int) -> Response:
        return Response(InboxCommentDetailSerializer(_comment(request, pk)).data)

    @extend_schema(
        request=TriageUpdateSerializer,
        responses={200: InboxCommentDetailSerializer},
        summary="Assign, close or reopen a comment, or set its intent",
    )
    def patch(self, request: Request, pk: int) -> Response:
        comment = _comment(request, pk)
        payload = TriageUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        actor = authenticated_user(request)

        if "status" in data or "assignee" in data:
            triage.update(
                comment,
                workspace=request_workspace(request),
                actor=actor,
                status=data.get("status"),
                **({"assignee": data["assignee"]} if "assignee" in data else {}),
            )
        if "intent" in data:
            coding.set_intent(comment, intent=data["intent"], actor=actor)
        return Response(InboxCommentDetailSerializer(_comment(request, pk)).data)


class InboxCommentNotesView(APIView):
    permission_classes = [IsAuthenticated, HasFlag(INBOX_S2), HasPermission(Permission.COMMENT)]

    @extend_schema(
        request=NoteWriteSerializer,
        responses={201: NoteSerializer},
        summary="Add an internal note (never sent to the platform)",
    )
    def post(self, request: Request, pk: int) -> Response:
        comment = _comment(request, pk)
        payload = NoteWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        note = triage.add_note(
            comment, actor=authenticated_user(request), body=payload.validated_data["body"]
        )
        return Response(NoteSerializer(note).data, status=status.HTTP_201_CREATED)


class InboxCommentReplyView(APIView):
    """Reply from the inbox: the same entitlement, pooled allowance and
    provider checks as `/analytics/comments/{id}/reply/` (L-4a) — this view
    adds only the inbox's own scoping and the `publish` permission, since the
    reply goes out in the brand's name."""

    permission_classes = [IsAuthenticated, HasFlag(INBOX_S2), HasPermission(Permission.PUBLISH)]

    @extend_schema(
        request=InboxReplySerializer,
        responses={201: AudienceReplyResultSerializer},
        summary="Reply to a comment (Advanced)",
    )
    def post(self, request: Request, pk: int) -> Response:
        comment = _comment(request, pk)
        payload = InboxReplySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        external_id = audience.reply_to(
            comment, body=payload.validated_data["body"], actor=authenticated_user(request)
        )
        return Response({"external_id": external_id}, status=status.HTTP_201_CREATED)


class SavedReplyViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[SavedReply]):
    serializer_class = SavedReplySerializer
    queryset = SavedReply.objects.all()
    pagination_class = DefaultPagination
    permission_classes: list[Any] = [IsAuthenticated, HasFlag(INBOX_S2)]

    def get_permissions(self) -> Any:
        needed = Permission.VIEW if self.action in ("list", "retrieve") else Permission.COMMENT
        return [*super().get_permissions(), HasPermission(needed)()]

    def perform_create(self, serializer: BaseSerializer[SavedReply]) -> None:
        serializer.save(
            workspace=request_workspace(self.request), created_by=authenticated_user(self.request)
        )
