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

import datetime as dt
from typing import Any

from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from analytics import webhooks
from analytics.models import (
    AudienceComment,
    AudienceDemographic,
    Report,
    ReportRun,
    RepurposeCandidate,
)
from analytics.serializers import (
    AudienceDemographicSerializer,
    AudienceReplyResultSerializer,
    AudienceReplySerializer,
    BestTimeSerializer,
    CommentSerializer,
    CompetitorComparisonSerializer,
    CompetitorSerializer,
    OverviewSerializer,
    ReportRunRequestSerializer,
    ReportRunSerializer,
    ReportSerializer,
    ReportShareLinkSerializer,
    ReportShareSerializer,
    RepurposeCandidateSerializer,
    SentimentSummarySerializer,
    SharedReportSerializer,
    TargetPerformanceSerializer,
)
from analytics.services import audience, report_share, reporting, repurposing, signals
from billing.permissions import HasFeature, HasFlag
from billing.services.entitlements import entitlements_for
from billing.services.flags import ANALYTICS_V6
from common.exceptions import OCCSError, StateConflict
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.throttling import IPTokenBucketThrottle
from common.workspaces import authenticated_user, request_workspace
from trends.services import competitors as competitor_service
from workspaces.models import Permission, Workspace
from workspaces.permissions import HasPermission

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
        return request_workspace(request)

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
                post_target__post__workspace=request_workspace(request)
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


# -----------------------------------------------------------------------------
# Reporting (Phase 6)
# -----------------------------------------------------------------------------
def _report_permissions(action: str) -> list[Any]:
    """Read with `analyze`, write with `edit`.

    A report is an analysis surface, so viewing one is the `analyze`
    permission rather than `view` — a member who may read posts is not
    thereby entitled to the workspace's performance.
    """
    read = action in {"list", "retrieve", "runs"}
    needed = Permission.ANALYZE if read else Permission.EDIT
    return [
        permission()
        for permission in (IsAuthenticated, HasFlag(ANALYTICS_V6), HasPermission(needed))
    ]


class ReportViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[Report]):
    """Saved report definitions, and the runs they produce (P6-04).

    The definition is declarative — `{kind, options}` sections — and rendering
    lives in `services.reporting`, so the screen and the PDF are one code path
    rather than two that agree today.
    """

    serializer_class = ReportSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.ANALYZE)]
    pagination_class = DefaultPagination
    queryset = Report.objects.select_related("created_by")

    def get_permissions(self) -> Any:
        return _report_permissions(self.action)

    def perform_create(self, serializer: BaseSerializer[Report]) -> None:
        serializer.save(
            workspace=request_workspace(self.request), created_by=authenticated_user(self.request)
        )

    @extend_schema(
        responses={200: ReportRunSerializer(many=True)},
        summary="Every rendering of this report",
    )
    @action(detail=True, methods=["get"], url_path="runs")
    def runs(self, request: Request, pk: str | None = None) -> Response:
        runs = self.get_object().runs.all()
        return Response(ReportRunSerializer(runs, many=True).data)

    @extend_schema(
        request=ReportRunRequestSerializer,
        responses={201: ReportRunSerializer, 402: None},
        summary="Render this report now",
        description=(
            "**402 when the window exceeds the plan's history horizon** — a "
            "client-facing document with a quietly clipped range is worse than "
            "an error, because nobody can tell by reading it (P6-09)."
        ),
    )
    @action(detail=True, methods=["post"], url_path="render")
    def render(self, request: Request, pk: str | None = None) -> Response:
        report = self.get_object()
        payload = ReportRunRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        ends_at = payload.validated_data.get("ends_at") or timezone.now()
        starts_at = payload.validated_data.get("starts_at") or (
            ends_at - dt.timedelta(days=report.window_days)
        )
        run = reporting.render_report(
            report,
            starts_at=starts_at,
            ends_at=ends_at,
            requested_by=authenticated_user(request),
        )
        return Response(ReportRunSerializer(run).data, status=status.HTTP_201_CREATED)


class _RunScopedView(APIView):
    """Anything addressed by a run id.

    One resolution, shared by the two views below rather than written twice:
    the queryset is filtered by workspace, so another tenant's run is a 404 and
    never a 403 (Part 7 rule 3).
    """

    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(ANALYTICS_V6),
        HasPermission(Permission.ANALYZE),
    ]

    def run_or_404(self, request: Request, pk: int) -> ReportRun:
        return get_object_or_404(
            ReportRun.objects.filter(report__workspace=request_workspace(request)), pk=pk
        )


class ReportRunShareView(_RunScopedView):
    """Share one rendered run and list who holds a link.

    **Revoke is not optional machinery**, for the same reason it is not on a
    `GUEST_VIEW` post link: a multi-use link cannot expire by being consumed,
    so without an explicit close there is no way to answer "stop that person
    seeing this" before the thirtieth day. It is the view below.
    """

    @extend_schema(
        responses={200: ReportShareLinkSerializer(many=True)},
        summary="Who this report has been shared with",
    )
    def get(self, request: Request, pk: int) -> Response:
        links = self.run_or_404(request, pk).share_links.all()
        return Response(ReportShareLinkSerializer(links, many=True).data)

    @extend_schema(
        request=ReportShareSerializer,
        responses={201: ReportShareLinkSerializer},
        summary="Share this report with someone who has no account",
        description=(
            "Returns the link row, **never the raw token** — that exists once, "
            "in the response body's `url`, so a later read of this list cannot "
            "replay it."
        ),
    )
    def post(self, request: Request, pk: int) -> Response:
        run = self.run_or_404(request, pk)
        payload = ReportShareSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            link, raw = report_share.issue(
                run,
                created_by=authenticated_user(request),
                email=payload.validated_data.get("email", ""),
            )
        except ValueError as error:
            # 409: the run exists and the caller may see it, but it is in the
            # wrong state. No permission and no upgrade changes that.
            raise StateConflict(str(error)) from error

        body = dict(ReportShareLinkSerializer(link).data)
        body["url"] = f"{settings.SITE_URL}/{report_share.ROUTE}/{raw}"
        return Response(body, status=status.HTTP_201_CREATED)


class ReportShareRevokeView(_RunScopedView):
    @extend_schema(
        request=None,
        responses={204: None},
        summary="Close a report share link",
        description="Idempotent — revoking a closed link is 204, not 409.",
    )
    def post(self, request: Request, pk: int, link_id: int) -> Response:
        run = self.run_or_404(request, pk)
        link = get_object_or_404(run.share_links, pk=link_id)
        report_share.revoke(link)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SharedReportThrottle(IPTokenBucketThrottle):
    """Generous, like the review one. A client reloading a report on a shaky
    connection must never see this; it is defence in depth against naive
    enumeration, which a 256-bit token already makes infeasible."""

    scope = "analytics:shared-report"
    capacity = 60
    refill_per_second = 1


class SharedReportView(APIView):
    """The public, token-scoped read (P6-06).

    No login — the token is the credential, exactly as on the reminder packet
    and the review packet. A token is not a workspace-scoped pk, so the tenancy
    sweep has nothing to walk here and `report_share.resolve` is the access
    control instead.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [SharedReportThrottle]

    @extend_schema(
        responses={200: SharedReportSerializer},
        summary="Read a report from a shared link",
        description=(
            "Returns the run's **frozen** payload. A figure somebody quoted in "
            "a meeting still says what it said, however the numbers have moved "
            "since."
        ),
        auth=[],
    )
    def get(self, request: Request, token: str) -> Response:
        link = report_share.resolve(token)
        if link is None:
            raise NotFound("This link is invalid, expired or has been revoked.")
        return Response(SharedReportSerializer(report_share.guest_context(link)).data)


class AudienceDemographicsView(_AnalyticsView):
    """Who follows this workspace's accounts (P6-01).

    The newest capture per `(account, dimension)`, including the `UNAVAILABLE`
    ones — a dimension the provider declined is a row that says so, because a
    surface cannot render "we cannot see this yet" for an axis it never
    received.
    """

    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(ANALYTICS_V6),
        HasPermission(Permission.ANALYZE),
    ]

    @extend_schema(
        responses={200: AudienceDemographicSerializer(many=True)},
        summary="Audience demographics per connected account",
    )
    def get(self, request: Request) -> Response:
        workspace = self.workspace(request)
        rows = (
            AudienceDemographic.objects.filter(social_account__workspace=workspace)
            .select_related("social_account")
            .order_by("social_account_id", "dimension", "-captured_at")
        )
        newest: dict[tuple[int, str], Any] = {}
        for row in rows:
            newest.setdefault((row.social_account_id, row.dimension), row)
        return Response(AudienceDemographicSerializer(list(newest.values()), many=True).data)


class _CompetitorView(_AnalyticsView):
    """Shared permissions for the three competitor routes.

    A base rather than one view inheriting another: subclassing a view that
    already declares `get`/`post` would silently publish those verbs on the
    child's URL too, which is how a detail route ends up answering a list.
    """

    permission_classes: list[Any] = [
        IsAuthenticated,
        HasFlag(ANALYTICS_V6),
        HasPermission(Permission.ANALYZE),
    ]


class CompetitorView(_CompetitorView):
    """The competitors this workspace watches (P6-08).

    A trend source kind behind the scenes — the same five stages, the same
    per-kind scoring partition — which is why there is no second pipeline to
    configure here.
    """

    @extend_schema(
        responses={200: CompetitorSerializer(many=True)},
        summary="Tracked competitor accounts",
    )
    def get(self, request: Request) -> Response:
        rows = competitor_service.tracked(self.workspace(request))
        return Response(CompetitorSerializer(rows, many=True).data)

    @extend_schema(
        request=CompetitorSerializer,
        responses={201: CompetitorSerializer, 402: None},
        summary="Start tracking a competitor",
        description=(
            "**402 at the plan's cap** — the number of tracked competitors is "
            "an admin-editable row, and each one costs a vendor call per "
            "refresh."
        ),
    )
    def post(self, request: Request) -> Response:
        payload = CompetitorSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        source = competitor_service.track(
            self.workspace(request),
            platform=payload.validated_data["platform"],
            handle=payload.validated_data["handle"],
            label=payload.validated_data.get("label", ""),
        )
        return Response(CompetitorSerializer(source).data, status=status.HTTP_201_CREATED)


class CompetitorDetailView(_CompetitorView):
    @extend_schema(
        responses={204: None},
        summary="Stop tracking a competitor",
        description=(
            "Deactivated, not deleted: what was already measured is what a past "
            "comparison was computed from."
        ),
    )
    def delete(self, request: Request, pk: int) -> Response:
        source = get_object_or_404(competitor_service.tracked(self.workspace(request)), pk=pk)
        competitor_service.untrack(source)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CompetitorComparisonView(_CompetitorView):
    @extend_schema(
        responses={200: CompetitorComparisonSerializer},
        summary="This workspace against the competitors it tracks",
        description=(
            "Both sides are interactions over audience — the same calculation, "
            "which is what makes them comparable. An unreported follower count "
            "gives a **null** rate, never a zero one."
        ),
    )
    def get(self, request: Request) -> Response:
        workspace = self.workspace(request)
        platform = request.query_params.get("platform", "")
        if not platform:
            raise ValidationError({"platform": "Name the platform to compare on."})

        competitor_service.refresh(workspace, platform=platform)
        result = competitor_service.comparison(workspace, platform=platform)
        return Response(CompetitorComparisonSerializer(result).data)
