"""Planning endpoints (Phase 3).

Every authority question is answered before a serializer sees a row: the
queryset narrows to the caller's workspace, and a service enforces the rest.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer

from billing.permissions import HasFlag
from billing.services.flags import PLANNING_V3
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from content.models import Post
from planning.models import BulkOperation, Campaign, Label, SavedView, Timetable
from planning.serializers import (
    BulkOperationRequestSerializer,
    BulkOperationSerializer,
    CampaignItemSerializer,
    CampaignSerializer,
    CampaignStatusRequestSerializer,
    LabelSerializer,
    SavedViewSerializer,
    TimetableSerializer,
)
from planning.services import bulk as bulk_service
from planning.services import campaigns as campaign_service
from planning.services import labels as label_service
from planning.services import views as view_service
from workspaces.models import Permission
from workspaces.permissions import HasPermission


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

    **Not gated by plan as a feature** — planning is how the product is used —
    but the *number* of campaigns is a quota, checked in the service so a task
    or a later endpoint inherits it.
    """

    serializer_class = CampaignSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.VIEW)]
    pagination_class = DefaultPagination
    queryset = Campaign.objects.select_related("brief", "created_by")

    def get_permissions(self) -> Any:
        return _view_or_edit_permissions(self.action)

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
