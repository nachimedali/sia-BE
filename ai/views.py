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
    HashtagSuggestionSerializer,
    ReviseRequestSerializer,
    VariantCommitSerializer,
    VariantSelectionSerializer,
    VoiceProfileSerializer,
)
from ai.services import hashtags
from ai.services import variants as variant_service
from ai.services.pipeline import create_generation
from ai.services.revisions import create_revision
from ai.tasks import run_generation_task
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.serializers import PostSerializer


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
        workspace = request_workspace(request)
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
            source_media=data.get("source_media"),
            paid_slots=data.get("paid_slots"),
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

    # ---- variant economics (X-09) -------------------------------------
    #
    # Two verbs on the generation rather than a `/variants/{id}/` resource:
    # the allowance is a property of the *generation*, and a per-variant
    # endpoint would have to re-derive it on every call, which is how the
    # number the user sees and the number that enforces drift apart.

    def _variant_action(self, request: Request, act: Any) -> Response:
        """The shared half of `select` and `unlock`.

        Both take the same body, act on the same object and answer with the
        same shape; only the verb differs. Written once so the next parameter
        one of them gains does not have to be remembered for the other.
        """
        generation = self.get_object()
        payload = VariantSelectionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        act(
            generation,
            variant_ids=payload.validated_data["variant_ids"],
            actor=authenticated_user(request),
        )
        # Re-read **through the viewset's own queryset**, not
        # `refresh_from_db()`. The bulk updates in the service bypass Python's
        # object cache so something must be re-read, but a bare refresh drops
        # the `variants__media_asset` prefetch and the serializer then issues
        # one query per variant — an N+1 on every click of the dock.
        fresh = self.get_queryset().get(pk=generation.pk)
        return Response(GenerationSerializer(fresh).data)

    @extend_schema(
        request=VariantSelectionSerializer,
        responses={200: GenerationSerializer},
        summary="Choose which variants to keep",
        description=(
            "Replaces the selection with exactly these variants. Answers 402 "
            "`variant_allowance_exceeded` past the paid slots, carrying the "
            "unlock price and how many need buying."
        ),
    )
    @action(detail=True, methods=["post"])
    def select(self, request: Request, pk: str | None = None) -> Response:
        return self._variant_action(request, variant_service.select)

    @extend_schema(
        request=VariantSelectionSerializer,
        responses={200: GenerationSerializer},
        summary="Buy surplus variants at the unlock price",
    )
    @action(detail=True, methods=["post"])
    def unlock(self, request: Request, pk: str | None = None) -> Response:
        return self._variant_action(request, variant_service.unlock)

    @extend_schema(
        request=VariantCommitSerializer,
        responses={201: PostSerializer(many=True)},
        summary="Send the selection to the calendar as drafts",
        description=(
            "One draft post per selected variant. With `scheduled_at` it goes "
            "through the schedule service, so the horizon, quota and approval "
            "gates apply exactly as they would to a hand-typed post. Nothing "
            "here publishes (L-2)."
        ),
    )
    @action(detail=True, methods=["post"])
    def commit(self, request: Request, pk: str | None = None) -> Response:
        generation = self.get_object()
        payload = VariantCommitSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        posts = variant_service.commit(
            generation,
            actor=authenticated_user(request),
            scheduled_at=payload.validated_data.get("scheduled_at"),
        )
        return Response(PostSerializer(posts, many=True).data, status=status.HTTP_201_CREATED)


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
        serializer.save(workspace=request_workspace(self.request))


class HashtagSuggestionView(APIView):
    """Hashtags that are actually working in this workspace's category (P1-13).

    A **read**, not a generation: no provider call, no credits, no quality
    gate. The corpus is shared per category (D11), so the answer is drawn from
    what the whole vertical is observably doing rather than from a model's
    guess — and each row carries the count it was ranked on, because a ranked
    list with no evidence is an opinion the caller cannot check.
    """

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        responses={200: HashtagSuggestionSerializer},
        summary="Hashtags ranked by use in this category",
    )
    def get(self, request: Request) -> Response:
        ranked = hashtags.rank_for_workspace(request_workspace(request))
        return Response({"hashtags": [{"tag": row.tag, "count": row.count} for row in ranked]})
