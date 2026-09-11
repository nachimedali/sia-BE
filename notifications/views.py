"""Notification endpoints (P2-12).

Plain paths, not a router registration: both answer for **the caller** in their
current workspace rather than for an id in the URL, so there is nothing here
for the tenancy sweep (A52) to walk — the same shape `WorkspaceSettingsView`
and the analytics reads already use.

Scoped by user *and* workspace on every query. Not only workspace: a
notification is addressed to one person, and a colleague reading it would be a
leak inside a tenant rather than across one.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from notifications import services
from notifications.models import Notification, NotificationPreference
from notifications.serializers import (
    MarkReadRequestSerializer,
    NotificationPreferenceSerializer,
    NotificationSerializer,
    preference_rows,
)


class NotificationListView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: NotificationSerializer(many=True)},
        summary="This person's notifications in this workspace",
        parameters=[
            OpenApiParameter("unread", bool, description="Only what has not been read yet.")
        ],
    )
    def get(self, request: Request) -> Response:
        queryset = Notification.objects.filter(
            user=authenticated_user(request), workspace=request_workspace(request)
        )
        if request.query_params.get("unread") in {"1", "true", "True"}:
            queryset = queryset.filter(read_at__isnull=True)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(NotificationSerializer(page, many=True).data)

    @extend_schema(
        request=MarkReadRequestSerializer,
        responses={200: None},
        summary="Mark notifications read",
        description="Omit `ids` to clear everything unread in this workspace.",
    )
    def post(self, request: Request) -> Response:
        payload = MarkReadRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        marked = services.mark_read(
            user=authenticated_user(request),
            workspace=request_workspace(request),
            ids=payload.validated_data.get("ids"),
        )
        return Response({"marked_read": marked})


class NotificationPreferenceView(APIView):
    """Which events reach this person, and how.

    `GET` returns a row for **every** event — stored or defaulted — because a
    settings screen has to draw one per event, and a client filling the gaps
    itself would be a second copy of the defaults to drift from.
    """

    permission_classes: list[Any] = [IsAuthenticated]

    def _stored(self, request: Request) -> dict[str, list[str]]:
        return {
            row.event_key: list(row.transports)
            for row in NotificationPreference.objects.filter(
                user=authenticated_user(request), workspace=request_workspace(request)
            )
        }

    @extend_schema(
        responses={200: NotificationPreferenceSerializer(many=True)},
        summary="Read notification preferences",
    )
    def get(self, request: Request) -> Response:
        return Response(preference_rows(self._stored(request)))

    @extend_schema(
        request=NotificationPreferenceSerializer,
        responses={200: NotificationPreferenceSerializer(many=True)},
        summary="Set one event's transports",
    )
    def put(self, request: Request) -> Response:
        payload = NotificationPreferenceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        NotificationPreference.objects.update_or_create(
            user=authenticated_user(request),
            workspace=request_workspace(request),
            event_key=payload.validated_data["event_key"],
            defaults={"transports": payload.validated_data["transports"]},
        )
        return Response(preference_rows(self._stored(request)))
