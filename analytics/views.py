"""Analytics endpoints (design.md §7, §8.9, §10.5).

Plain paths rather than a router registration, and deliberately so: none of
these returns a workspace-scoped object by pk. Every one derives its answer from
the *caller's own* workspace, so there is no id in a URL for the tenancy sweep
(A52) to try to leak — the two that do take a pk (`repurpose/{id}/accept|
dismiss`) resolve it through a workspace-filtered queryset, which is the same
guarantee the mixin gives, applied where the mixin does not reach.

**Workspace and horizon are resolved once**, on `_AnalyticsView`, so eight
endpoints cannot drift apart on either. §4.1 gives Free 7 days of history, Pro 90
and Advanced 730; a Free workspace therefore sees a real but short window rather
than a 402 — reading your own numbers is not a paid feature, keeping two years of
them is.

Repurposing *is* a paid feature (§4.1), so those three endpoints carry
`HasFeature("repurposing")` — the shared factory, not an inline check.
"""

from __future__ import annotations

from typing import Any

from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics import webhooks
from analytics.models import AudienceComment, RepurposeCandidate
from analytics.serializers import (
    AudienceReplyResultSerializer,
    AudienceReplySerializer,
    BestTimeSerializer,
    CommentSerializer,
    OverviewSerializer,
    RepurposeCandidateSerializer,
    SentimentSummarySerializer,
    TargetPerformanceSerializer,
)
from analytics.services import audience, repurposing, signals
from billing.permissions import HasFeature
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError
from common.workspaces import active_workspace
from workspaces.models import Workspace

REPURPOSE_FEATURE = "repurposing"

#: How many comments the feed returns. Not `DefaultPagination`: this is a
#: fixed-size recent-activity strip on the analytics page, not a browsable
#: collection, so a `next` link would be a contract nothing consumes.
COMMENT_FEED_LIMIT = 50


class _AnalyticsView(APIView):
    """Every read here answers for the caller's own workspace, bounded by the
    caller's own plan. Both resolutions live here rather than in each view."""

    permission_classes: list[Any] = [IsAuthenticated]

    def workspace(self, request: Request) -> Workspace:
        return active_workspace(request)

    def horizon(self, workspace: Workspace) -> int:
        return entitlements_for(workspace).analytics_horizon_days()

    def rows(self, request: Request) -> list[signals.TargetPerformance]:
        workspace = self.workspace(request)
        return signals.performance(workspace.pk, horizon_days=self.horizon(workspace))


class _RepurposeView(_AnalyticsView):
    """Repurposing is paid (§4.1) — a hard 402 with an upgrade payload, unlike
    the reads above, which every plan gets some window of."""

    permission_classes: list[Any] = [IsAuthenticated, HasFeature(REPURPOSE_FEATURE)]


class AnalyticsOverviewView(_AnalyticsView):
    @extend_schema(
        responses={200: OverviewSerializer},
        summary="Headline numbers, top posts, best times and format attribution",
        description=(
            "Everything the analytics screen needs in one call — `best_times` is "
            "included here so rendering the page does not run the same scan twice."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = self.workspace(request)
        result = signals.overview(workspace.pk, horizon_days=self.horizon(workspace))
        return Response(OverviewSerializer(result).data)


class AnalyticsPostsView(_AnalyticsView):
    @extend_schema(
        responses={200: TargetPerformanceSerializer(many=True)},
        summary="Every published copy inside the plan's history horizon",
    )
    def get(self, request: Request) -> Response:
        return Response(TargetPerformanceSerializer(self.rows(request), many=True).data)


class AnalyticsBestTimesView(_AnalyticsView):
    @extend_schema(
        responses={200: BestTimeSerializer(many=True)},
        summary="Engagement by weekday and hour",
        description="Buckets with a single observation are omitted — one post "
        "is a coincidence, not a best time.",
    )
    def get(self, request: Request) -> Response:
        buckets = signals.best_times(self.rows(request))
        return Response(BestTimeSerializer(buckets, many=True).data)


class AnalyticsSentimentView(_AnalyticsView):
    @extend_schema(
        responses={200: SentimentSummarySerializer},
        summary="Comment sentiment across this workspace's published posts",
    )
    def get(self, request: Request) -> Response:
        workspace = self.workspace(request)
        totals = signals.sentiment_summary(workspace.pk, horizon_days=self.horizon(workspace))
        return Response(SentimentSummarySerializer(totals).data)


class AnalyticsCommentsView(_AnalyticsView):
    @extend_schema(
        responses={200: CommentSerializer(many=True)},
        summary="The most recent comments, newest first",
    )
    def get(self, request: Request) -> Response:
        comments = AudienceComment.objects.measured().filter(
            post_target__post__workspace=self.workspace(request)
        )[:COMMENT_FEED_LIMIT]
        return Response(CommentSerializer(comments, many=True).data)


class AudienceCommentReplyView(APIView):
    """`POST /analytics/comments/{id}/reply/` — Advanced only (L-4a, P0-36).

    The gate is commercial, not technical: reading is free from the provider
    and outbound is nearly so, but answering an audience comment without
    leaving the app is what the top plan sells. Lower plans read, analyse, and
    reply in the native app — they are not refused the *comment*, only the
    reply.

    402 at allowance exhaustion, pooled across the organization, so one
    workspace cannot spend another's headroom.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=AudienceReplySerializer,
        responses={201: AudienceReplyResultSerializer},
        summary="Reply to an audience comment",
    )
    def post(self, request: Request, pk: int) -> Response:
        # Workspace-filtered lookup, so another tenant's comment id is a 404
        # rather than a 403 — the queryset is the boundary, not a check after
        # the fetch (Part 7 rule 3).
        comment = get_object_or_404(
            AudienceComment.objects.measured().filter(
                post_target__post__workspace=active_workspace(request)
            ),
            pk=pk,
        )
        payload = AudienceReplySerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        external_id = audience.reply_to(
            comment, body=payload.validated_data["body"], actor=request.user
        )
        return Response({"external_id": external_id}, status=status.HTTP_201_CREATED)


class ZernioCommentWebhookView(APIView):
    """`POST /webhooks/zernio/comment/` — the Advanced tier's capture path.

    Unauthenticated by necessity, authenticated by signature, exactly like the
    Stripe handler. Always 200 on a verified body, even when nothing was
    stored: a provider treats a non-2xx as "retry", and there is nothing to
    retry about a comment for a post we do not publish.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @csrf_exempt
    @extend_schema(request=None, responses={200: None}, summary="Zernio comment webhook")
    def post(self, request: Request) -> Response:
        signature = request.META.get("HTTP_X_ZERNIO_SIGNATURE", "")
        try:
            webhooks.verify(request.body, signature)
        except webhooks.WebhookSignatureError as exc:
            raise OCCSError(
                "Webhook signature verification failed.", code="invalid_signature"
            ) from exc

        payload = request.data if isinstance(request.data, dict) else {}
        stored = webhooks.ingest_comment_event(payload)
        return Response({"received": True, "stored": stored}, status=status.HTTP_200_OK)


class RepurposeQueueView(_RepurposeView):
    @extend_schema(
        responses={200: RepurposeCandidateSerializer(many=True)},
        summary="Old posts worth running again, best first",
    )
    def get(self, request: Request) -> Response:
        candidates = (
            RepurposeCandidate.objects.filter(
                post__workspace=self.workspace(request),
                dismissed_at__isnull=True,
                reissued_post__isnull=True,
            )
            .select_related("post")
            .order_by("-score")
        )
        return Response(RepurposeCandidateSerializer(candidates, many=True).data)


class RepurposeAcceptView(_RepurposeView):
    @extend_schema(
        request=None,
        responses={200: RepurposeCandidateSerializer},
        summary="Accept a suggestion, opening a draft that carries its origin",
    )
    def post(self, request: Request, pk: int) -> Response:
        candidate = _candidate(self.workspace(request), pk)
        # `IsAuthenticated` has already run, so this is never None — the
        # assertion is for mypy, which cannot see that from the permission class.
        author_id = request.user.pk
        assert author_id is not None
        repurposing.accept(candidate, author_id=author_id)
        return Response(RepurposeCandidateSerializer(candidate).data)


class RepurposeDismissView(_RepurposeView):
    @extend_schema(
        request=None,
        responses={200: RepurposeCandidateSerializer},
        summary="Dismiss a suggestion",
    )
    def post(self, request: Request, pk: int) -> Response:
        candidate = _candidate(self.workspace(request), pk)
        repurposing.dismiss(candidate)
        return Response(RepurposeCandidateSerializer(candidate).data)


def _candidate(workspace: Workspace, pk: int) -> RepurposeCandidate:
    """Workspace-filtered before the pk is honoured, so another workspace's
    candidate is a 404 rather than a 403 (A9) — the same answer the shared
    tenancy mixin gives for the ViewSets it covers."""
    return get_object_or_404(
        RepurposeCandidate.objects.select_related("post", "post__workspace"),
        pk=pk,
        post__workspace=workspace,
    )
