"""Generation endpoints (design.md §7).

Views parse and serialise; `ai.services` decides (implementation.md §4.1). The
one thing a view does that a service call does not: `GenerateView` enqueues
`ai.tasks.run_generation_task` rather than calling `pipeline.run_generation`
inline — no provider call happens inside a request/response cycle (design.md
§11). `GenerationViewSet` and `VoiceProfileViewSet` are registered on the
router, so `test_cross_workspace_access_returns_404_on_every_viewset` (A52)
covers both automatically; `GenerateView` is a create-only `APIView` in the
same shape billing's create-only views use (it never looks up another
workspace's row, so there is nothing for that sweep to catch here).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import Generation, VoiceProfile
from ai.permissions import HasSufficientCredits
from ai.serializers import (
    GenerateRequestSerializer,
    GenerationSerializer,
    ReviseRequestSerializer,
    VoiceProfileSerializer,
)
from ai.services.pipeline import create_generation
from ai.services.revisions import create_revision
from ai.tasks import run_generation_task
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace, authenticated_user


class GenerateView(APIView):
    # HasSufficientCredits is I5's second gate (design.md §8.1) — the third
    # and fourth are `pipeline.create_generation`'s own check and
    # `GET /billing/entitlements/`; the serializer is the first
    # (`GenerateRequestSerializer.validate`). All four are independent by
    # design — see `ai/tests/test_entitlement_gates.py`.
    permission_classes: list[Any] = [IsAuthenticated, HasSufficientCredits]

    @extend_schema(
        request=GenerateRequestSerializer,
        responses={201: GenerationSerializer},
        summary="Start a generation",
        description=(
            "Creates a PENDING generation and queues it on ai_q — no provider "
            "call happens inline (design.md §11). Poll GET /ai/generations/{id}/ "
            "for the result."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = active_workspace(request)
        serializer = GenerateRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        generation = create_generation(
            workspace=workspace,
            user=authenticated_user(request),
            kind=data["kind"],
            mode=data["mode"],
            prompt=data["prompt"],
            product=data.get("product"),
            voice_profile=data.get("voice_profile"),
            aspect=data["aspect"],
            render_style=data["render_style"],
            scene=data["scene"],
            is_batch=data["is_batch"],
        )
        run_generation_task.delay(generation_id=generation.id, n=data["n"])
        # A no-op in production (the task runs on a worker, asynchronously,
        # so this still reads PENDING) but load-bearing under
        # CELERY_TASK_ALWAYS_EAGER=True (config/settings/test.py): eager
        # mode runs the task inline inside `.delay()`, above, and without
        # this the response would serialise the stale pre-task instance.
        generation.refresh_from_db()
        return Response(GenerationSerializer(generation).data, status=status.HTTP_201_CREATED)


class GenerationViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[Generation],
):
    serializer_class = GenerationSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    queryset = Generation.objects.select_related("product", "voice_profile").prefetch_related(
        "variants__media_asset"
    )

    @extend_schema(
        request=ReviseRequestSerializer,
        responses={201: GenerationSerializer},
        summary="Revise a generation",
        description="Cheaper than a fresh generation (design.md §8.3) — 1 credit.",
    )
    @action(detail=True, methods=["post"])
    def revise(self, request: Request, pk: str | None = None) -> Response:
        parent = self.get_object()
        serializer = ReviseRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        child = create_revision(
            parent=parent, user=authenticated_user(request), instructions=data["instructions"]
        )
        run_generation_task.delay(generation_id=child.id, n=data["n"])
        child.refresh_from_db()  # see GenerateView.post
        return Response(GenerationSerializer(child).data, status=status.HTTP_201_CREATED)


class VoiceProfileViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[VoiceProfile],
):
    serializer_class = VoiceProfileSerializer
    permission_classes: list[Any] = [IsAuthenticated]
    pagination_class = DefaultPagination
    queryset = VoiceProfile.objects.all()

    def perform_create(self, serializer: Any) -> None:
        serializer.save(workspace=active_workspace(self.request))
