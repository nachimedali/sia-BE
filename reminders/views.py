"""Reminder endpoints (design.md §7, §8.5).

Two shapes. `ReminderViewSet` is workspace-scoped and authenticated, powering
the `/app/calendar` list — read-only, since every state change on a Reminder
happens through the token-scoped views below, driven by the emailed link, not
this dashboard. `ReminderPacketView`/`ReminderConfirmView`/
`ReminderSnoozeView`/`ReminderSkipView` are keyed by the token instead of a
workspace-scoped pk — reachable from `/r/[token]` with no login — and each
resolves through `reminders.services.resolve_token`, the one lookup that
makes "expose nothing beyond this reminder's own packet" true. They are
deliberately not registered on `router` (design.md A52): a token is not a
workspace-scoped pk, so the cross-workspace tenancy sweep has nothing to walk
here — resolution is the access control.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.throttling import IPTokenBucketThrottle
from reminders import services
from reminders.models import Reminder
from reminders.serializers import (
    ReminderPacketSerializer,
    ReminderSerializer,
    ReminderSnoozeRequestSerializer,
)


class ReminderTokenThrottle(IPTokenBucketThrottle):
    # Generous — a real user reloading the packet on a shaky phone connection
    # should never see this. It exists only as defense in depth against naive
    # enumeration; a 256-bit `secrets.token_urlsafe(32)` already makes
    # guessing one infeasible on its own.
    scope = "reminders:token"
    capacity = 60
    refill_per_second = 1


class ReminderViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[Reminder],
):
    serializer_class = ReminderSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    workspace_field = "post__workspace"
    queryset = Reminder.objects.select_related("post").order_by("send_at")


def _resolve_or_404(token: str) -> Reminder:
    reminder = services.resolve_token(token)
    if reminder is None:
        raise NotFound("This link is invalid or has expired.")
    return reminder


class ReminderPacketView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [ReminderTokenThrottle]

    @extend_schema(
        responses={200: ReminderPacketSerializer},
        summary="Fetch a reminder's publish packet",
        description="No login (design.md §8.5) — the token is the credential.",
        auth=[],
    )
    def get(self, request: Request, token: str) -> Response:
        reminder = _resolve_or_404(token)
        return Response(ReminderPacketSerializer(reminder).data)


class ReminderConfirmView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [ReminderTokenThrottle]

    @extend_schema(
        request=None,
        responses={200: ReminderPacketSerializer},
        summary="Mark a reminder's post as posted",
        auth=[],
    )
    def post(self, request: Request, token: str) -> Response:
        reminder = services.confirm(_resolve_or_404(token))
        return Response(ReminderPacketSerializer(reminder).data)


class ReminderSnoozeView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [ReminderTokenThrottle]

    @extend_schema(
        request=ReminderSnoozeRequestSerializer,
        responses={200: ReminderPacketSerializer},
        summary="Snooze a reminder to a new time",
        auth=[],
    )
    def post(self, request: Request, token: str) -> Response:
        reminder = _resolve_or_404(token)
        payload = ReminderSnoozeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        reminder = services.snooze(reminder, payload.validated_data["snoozed_to"])
        return Response(ReminderPacketSerializer(reminder).data)


class ReminderSkipView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [ReminderTokenThrottle]

    @extend_schema(
        request=None,
        responses={200: ReminderPacketSerializer},
        summary="Skip a reminder; the post returns to draft",
        auth=[],
    )
    def post(self, request: Request, token: str) -> Response:
        reminder = services.skip(_resolve_or_404(token))
        return Response(ReminderPacketSerializer(reminder).data)
