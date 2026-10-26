"""Planning endpoints (Phase 3).

Every authority question is answered before a serializer sees a row: the
queryset narrows to the caller's workspace, and a service enforces the rest.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Case, Count, Q, Value, When
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from billing.permissions import HasFeature, HasFlag
from billing.services.flags import CAMPAIGN_BRIEF_S5, PLANNING_V3
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.models import Post
from planning.models import BulkOperation, Campaign, CampaignStatus, Label, SavedView, Timetable
from planning.serializers import (
    BulkOperationRequestSerializer,
    BulkOperationSerializer,
    CampaignBriefSerializer,
    CampaignBriefWriteSerializer,
    CampaignCountsSerializer,
    CampaignItemSerializer,
    CampaignPlanSerializer,
    CampaignProgressDetailSerializer,
    CampaignSerializer,
    CampaignStatusRequestSerializer,
    LabelSerializer,
    PlanEstimateSerializer,
    PlanStartRequestSerializer,
    SavedViewSerializer,
    SeedResultSerializer,
    TimetableSerializer,
)
from planning.services import briefs as brief_service
from planning.services import bulk as bulk_service
from planning.services import campaigns as campaign_service
from planning.services import labels as label_service
from planning.services import plans as plan_service
from planning.services import progress as progress_service
from planning.services import views as view_service
from workspaces.models import Permission
from workspaces.permissions import HasPermission

#: The campaigns page (2026-10-26, owner): off on Spark.
CAMPAIGNS_FEATURE = "campaigns"


def _view_or_edit_permissions(action: str) -> list[Any]:
    """Read with `view`, write with `edit` — called from every planning
    ViewSet's `get_permissions` below rather than each computing it inline.

    A module-level function rather than a shared base class: a mixin
    declaring `permission_classes` sits outside every ViewSet's own hierarchy
    down to `APIView`, and mypy's cross-hierarchy override check flags that as
    incompatible even though the runtime behaviour is exactly what every
    concrete class here already declared correctly on its own. A function has
    no such attribute to conflict.

    Found in review: `BulkOperationViewSet` required `edit` for `list`/
    `retrieve` too, contradicting its own docstring ("Create-and-read only")
    — a `view`-only member could not read a batch's progress at all.
    `CampaignViewSet` carried a working but separately-written copy of this
    same split; both now call this one function.
    """
    read = action in {"list", "retrieve"}
    needed = Permission.VIEW if read else Permission.EDIT
    return [
        permission()
        for permission in (IsAuthenticated, HasFlag(PLANNING_V3), HasPermission(needed))
    ]


class CampaignViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[Campaign]):
    """Themes and sprints (P3-04).

    Gated by plan as a page (`campaigns`, off on Spark since 2026-10-26, owner),
    and the *number* of campaigns is a quota, checked in the service so a task
    or a later endpoint inherits it.
    """

    serializer_class = CampaignSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = Campaign.objects.select_related("brief", "created_by")

    #: steps-plan S5's routes: absent (404) with the flag off.
    S5_ACTIONS = frozenset({"brief", "plan", "estimate", "seed", "progress", "counts"})

    def get_permissions(self) -> Any:
        # A read of an S5 sub-resource is a read: `view` suffices, like the list.
        read = self.action in self.S5_ACTIONS and self.request.method in SAFE_METHODS
        permissions = _view_or_edit_permissions("retrieve" if read else self.action)
        if self.action in self.S5_ACTIONS:
            permissions.append(HasFlag(CAMPAIGN_BRIEF_S5)())
        # Last, so a flag that is off still answers 404 before any 402.
        permissions.append(HasFeature(CAMPAIGNS_FEATURE)())
        return permissions

    def get_queryset(self) -> Any:
        """Filtered, searched and ordered **on the server** (S5): the list is
        paginated, so a filter over the page already loaded would be wrong past
        the first page."""
        queryset = super().get_queryset()
        if self.action != "list":
            return queryset
        params = self.request.query_params
        status_filter = params.get("status", "").upper()
        if status_filter in CampaignStatus.values:
            queryset = queryset.filter(status=status_filter)
        search = params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(created_by__first_name__icontains=search)
                | Q(created_by__last_name__icontains=search)
                | Q(created_by__email__icontains=search)
            )
        metric = params.get("metric", "").strip()
        if metric:
            queryset = queryset.filter(goals__0__metric=metric)
        ordering = params.get("ordering", "")
        if ordering == "active_first":
            rank = Case(
                When(status=CampaignStatus.ACTIVE, then=Value(0)),
                When(status=CampaignStatus.PLANNING, then=Value(1)),
                default=Value(2),
            )
            queryset = queryset.order_by(rank, "-starts_at", "-id")
        elif ordering in {"starts_at", "-starts_at", "name", "-updated_at"}:
            queryset = queryset.order_by(ordering, "-id")
        return queryset

    def perform_create(self, serializer: BaseSerializer[Campaign]) -> None:
        data = dict(serializer.validated_data)
        brief = data.pop("brief", None)
        serializer.instance = campaign_service.create_campaign(
            workspace=request_workspace(self.request),
            actor=authenticated_user(self.request),
            **data,
        )
        if brief is not None:
            campaign_service.set_brief(serializer.instance, brief)

    def perform_update(self, serializer: BaseSerializer[Campaign]) -> None:
        assert serializer.instance is not None
        data = dict(serializer.validated_data)
        if "brief" in data:
            campaign_service.set_brief(serializer.instance, data.pop("brief"))
        for field, value in data.items():
            setattr(serializer.instance, field, value)
        serializer.instance.save()

    @extend_schema(
        request=CampaignStatusRequestSerializer,
        responses={200: CampaignSerializer},
        summary="Advance a campaign's lifecycle",
        description=(
            "`PLANNING → ACTIVE → CLOSED`, one direction only. An illegal "
            "transition is **409**, never 403 — no permission fixes it."
        ),
    )
    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request: Request, pk: str | None = None) -> Response:
        campaign = self.get_object()
        payload = CampaignStatusRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        campaign = campaign_service.advance(
            campaign, payload.validated_data["status"], actor=authenticated_user(request)
        )
        return Response(CampaignSerializer(campaign).data)

    @extend_schema(
        responses={200: CampaignCountsSerializer},
        summary="How many campaigns are in each status",
    )
    @action(detail=False, methods=["get"], url_path="counts")
    def counts(self, request: Request) -> Response:
        rows = dict(super().get_queryset().values_list("status").annotate(n=Count("id")).order_by())
        body = {value: rows.get(value, 0) for value in CampaignStatus.values}
        return Response({"ALL": sum(body.values()), **body})

    @extend_schema(
        methods=["GET"],
        responses={200: CampaignBriefSerializer},
        summary="Read the campaign brief and the proof this workspace can offer",
    )
    @extend_schema(
        methods=["PUT"],
        request=CampaignBriefWriteSerializer,
        responses={200: CampaignBriefSerializer},
        summary="Save the campaign brief (partial: only the keys sent change)",
        description="409 `campaign_closed` once the campaign is closed.",
    )
    @action(detail=True, methods=["get", "put"], url_path="brief")
    def brief(self, request: Request, pk: str | None = None) -> Response:
        campaign = self.get_object()
        if request.method == "PUT":
            payload = CampaignBriefWriteSerializer(data=request.data)
            payload.is_valid(raise_exception=True)
            brief_service.save(campaign, payload.validated_data, actor=authenticated_user(request))
            campaign.refresh_from_db()
        return Response(CampaignBriefSerializer(brief_service.read(campaign)).data)

    @extend_schema(
        request=None,
        responses={200: PlanEstimateSerializer},
        summary="Price a plan before anything is spent",
        description="400 `brief_incomplete` lists what is missing.",
    )
    @action(detail=True, methods=["post"], url_path="plan/estimate")
    def estimate(self, request: Request, pk: str | None = None) -> Response:
        return Response(PlanEstimateSerializer(plan_service.estimate(self.get_object())).data)

    @extend_schema(
        methods=["GET"],
        responses={200: CampaignPlanSerializer, 204: None},
        summary="The latest plan, with each calendar slot's draft",
    )
    @extend_schema(
        methods=["POST"],
        request=PlanStartRequestSerializer,
        responses={202: CampaignPlanSerializer},
        summary="Confirm the estimate and write the plan in the background",
        description=(
            "409 `estimate_changed` carries the new estimate; 409 `plan_in_progress`; "
            "402 `insufficient_credits`. Credits are held now and taken once, on success."
        ),
    )
    @action(detail=True, methods=["get", "post"], url_path="plan")
    def plan(self, request: Request, pk: str | None = None) -> Response:
        campaign = self.get_object()
        if request.method == "GET":
            latest = plan_service.latest(campaign)
            if latest is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(CampaignPlanSerializer(plan_service.read(latest)).data)

        payload = PlanStartRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        started = plan_service.start(
            campaign,
            user=authenticated_user(request),
            estimate_id=payload.validated_data["estimate_id"],
        )
        return Response(
            CampaignPlanSerializer(plan_service.read(started)).data,
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        request=None,
        responses={200: SeedResultSerializer},
        summary="Seed the calendar: one draft per plan slot, never scheduled",
        description="Idempotent — seeding the same plan twice creates nothing new.",
    )
    @action(detail=True, methods=["post"], url_path="seed")
    def seed(self, request: Request, pk: str | None = None) -> Response:
        result = plan_service.seed(self.get_object(), actor=authenticated_user(request))
        return Response(SeedResultSerializer(result).data)

    @extend_schema(
        responses={200: CampaignProgressDetailSerializer},
        summary="Measured progress against the goal, day by day; gaps are null",
    )
    @action(detail=True, methods=["get"], url_path="progress")
    def progress(self, request: Request, pk: str | None = None) -> Response:
        return Response(
            CampaignProgressDetailSerializer(progress_service.detail(self.get_object())).data
        )

    @extend_schema(
        request=CampaignItemSerializer,
        responses={201: CampaignItemSerializer},
        summary="Add a post to this campaign",
    )
    @action(detail=True, methods=["post"], url_path="items")
    def add_item(self, request: Request, pk: str | None = None) -> Response:
        campaign = self.get_object()
        payload = CampaignItemSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)

        item = campaign_service.add_post(
            campaign, payload.validated_data["post"], actor=authenticated_user(request)
        )
        return Response(CampaignItemSerializer(item).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        responses={204: None},
        summary="Remove a post from this campaign",
        description="Idempotent — removing what is not there is 204, not 404.",
    )
    @action(detail=True, methods=["delete"], url_path=r"items/(?P<post_id>\d+)")
    def remove_item(
        self, request: Request, pk: str | None = None, post_id: str | None = None
    ) -> Response:
        campaign = self.get_object()
        # Scoped by the campaign's own workspace, so another tenant's post id
        # simply matches nothing rather than being reachable here.
        lookup: Any = post_id
        post = Post.objects.filter(pk=lookup, workspace=campaign.workspace).first()
        if post is not None:
            campaign_service.remove_post(campaign, post)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _WorkspaceOwnedViewSet(WorkspaceScopedQuerySetMixin, viewsets.ModelViewSet[Any]):
    """The three planning collections below differ only in model and
    serializer, so the scoping lives here once; the permission split is the
    shared `_view_or_edit_permissions` function, not inherited state."""

    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination

    def get_permissions(self) -> Any:
        return _view_or_edit_permissions(self.action)


class LabelViewSet(_WorkspaceOwnedViewSet):
    """Colour labels (P3-10)."""

    serializer_class = LabelSerializer
    queryset = Label.objects.all()

    def perform_create(self, serializer: BaseSerializer[Label]) -> None:
        data = serializer.validated_data
        serializer.instance = label_service.create_label(
            workspace=request_workspace(self.request),
            name=data["name"],
            colour=data["colour"],
        )


class SavedViewViewSet(_WorkspaceOwnedViewSet):
    """Saved filters (P3-10)."""

    serializer_class = SavedViewSerializer
    queryset = SavedView.objects.all()

    def perform_create(self, serializer: BaseSerializer[SavedView]) -> None:
        data = serializer.validated_data
        serializer.instance = view_service.create_saved_view(
            workspace=request_workspace(self.request),
            name=data["name"],
            filters=data.get("filters", {}),
        )


class TimetableViewSet(_WorkspaceOwnedViewSet):
    """Preferred posting times, in local wall time (P3-11)."""

    serializer_class = TimetableSerializer
    queryset = Timetable.objects.all()

    def perform_create(self, serializer: BaseSerializer[Timetable]) -> None:
        serializer.save(workspace=request_workspace(self.request))


class BulkOperationViewSet(
    WorkspaceScopedQuerySetMixin,
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet[BulkOperation],
):
    """Start a bulk operation, then watch it (P3-12).

    **Create-and-read only.** An operation is a record of what happened; there
    is no coherent meaning for editing one, and deleting the evidence of a
    half-failed batch is precisely what the P3-G1 gate exists to prevent.

    Reading needs `view`, not `edit` (found in review): a static
    `permission_classes` here used to require `edit` for a plain `list`/
    `retrieve` too — a `view`-only member could not poll a batch's progress
    at all, contradicting "create-and-read only" above.
    """

    serializer_class = BulkOperationSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = BulkOperation.objects.prefetch_related("items")

    def get_permissions(self) -> Any:
        return _view_or_edit_permissions(self.action)

    @extend_schema(
        request=BulkOperationRequestSerializer,
        responses={202: BulkOperationSerializer},
        summary="Run an action over many posts",
        description=(
            "**202, not 201**: the rows are planned synchronously and the work "
            "happens on the queue, one task per item. Poll the returned "
            "operation for per-item outcomes — a partly failed batch reports "
            "`PARTIAL` with the reason on each item that did not work."
        ),
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = BulkOperationRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        operation = bulk_service.plan_operation(
            workspace=request_workspace(request),
            actor=authenticated_user(request),
            action=data["action"],
            post_ids=data.get("post_ids"),
            filters=data.get("filters"),
            payload=data.get("payload", {}),
        )
        bulk_service.dispatch(operation)
        return Response(BulkOperationSerializer(operation).data, status=status.HTTP_202_ACCEPTED)
