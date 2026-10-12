"""Playbook endpoints. Views parse and serialise; `playbook.services` decides.

The corpus is public posts shared by every workspace in a market — like the
trend corpus, there is no workspace-scoped row to read here, and the market
comes from the caller's own workspace, never from a parameter. What *is* the
workspace's — the product a composition uses, the generation it creates — is
looked up inside the request's workspace, so another tenant's id is a 404.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.serializers import GenerationSerializer
from billing.permissions import HasFlag
from billing.services.flags import PLAYBOOK_V1
from common.workspaces import authenticated_user, request_workspace
from playbook.serializers import (
    ComposeRequestSerializer,
    PlaybookPostSerializer,
    PlaybookVocabularySerializer,
    PlaybookWallSerializer,
    vocabulary,
)
from playbook.services import compose, query
from workspaces.models import Permission
from workspaces.permissions import HasPermission

_READ: list[Any] = [IsAuthenticated, HasFlag(PLAYBOOK_V1), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(PLAYBOOK_V1), HasPermission(Permission.EDIT)]


class PlaybookWallView(APIView):
    permission_classes = _READ

    @extend_schema(
        parameters=[
            OpenApiParameter("category", str, description="Root category slugs, comma-separated."),
            OpenApiParameter(
                "platform", str, description="youtube, google_trends; comma-separated."
            ),
            OpenApiParameter("kind", str, description="SHORT, VIDEO or SEARCH."),
            OpenApiParameter("posted", str, description="24h, 7d or 30d."),
            OpenApiParameter("q", str, description="Words in the title, caption or creator."),
            OpenApiParameter("sort", str, description="trending, views, engagement or newest."),
            OpenApiParameter("page", int),
        ],
        responses={200: PlaybookWallSerializer},
        summary="Trending public posts in this workspace's market",
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        data = query.wall(workspace, query.parse(request.query_params))
        return Response(PlaybookWallSerializer(data).data)


class PlaybookPostView(APIView):
    permission_classes = _READ

    @extend_schema(responses={200: PlaybookPostSerializer}, summary="One Playbook post")
    def get(self, request: Request, pk: int) -> Response:
        request_workspace(request)
        row = query.post(pk)
        if row is None:
            raise NotFound
        return Response(PlaybookPostSerializer(row).data)


class PlaybookVocabularyView(APIView):
    permission_classes = _READ

    @extend_schema(responses={200: PlaybookVocabularySerializer}, summary="Playbook labels")
    def get(self, request: Request) -> Response:
        request_workspace(request)
        return Response(PlaybookVocabularySerializer(vocabulary()).data)


class PlaybookComposeView(APIView):
    permission_classes = _WRITE

    @extend_schema(
        request=ComposeRequestSerializer,
        responses={201: GenerationSerializer},
        summary="Compose a product picture from Playbook references",
        description=(
            "Creates a PENDING `RECIPE` generation and queues it on ai_q. Poll "
            "GET /ai/generations/{id}/ for the result. Credits are debited only "
            "once the quality gate passes."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = ComposeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        generation = compose.compose(
            workspace=workspace, user=authenticated_user(request), data=payload.validated_data
        )
        return Response(GenerationSerializer(generation).data, status=status.HTTP_201_CREATED)
