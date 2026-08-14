"""Collaboration endpoints (design.md §7, §8.8; implementation.md Phase 13).

`MembershipViewSet` is router-registered (`workspaces/members`) because it
returns objects by pk — the Phase 4 tenancy sweep (design.md A52) has to be
able to walk it. `WorkspaceSettingsView` and `AuditLogView` are plain paths,
the same shape `analytics/views.py` uses for reads that answer for the
caller's own workspace rather than an id in the URL.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.permissions import HasFeature
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import active_workspace, authenticated_user
from workspaces.models import AuditLog, Membership, Role
from workspaces.permissions import HasRole
from workspaces.serializers import (
    AuditLogSerializer,
    MemberAddSerializer,
    MembershipRoleUpdateSerializer,
    MembershipSerializer,
    WorkspaceSettingsSerializer,
)
from workspaces.services import approvals, membership

#: Same feature as the approval-action endpoints (`content/views.py`) — the
#: settings toggle and the audit trail that records what it did are both part
#: of the Advanced-only collaboration surface.
APPROVAL_FEATURE = "approval_workflow"


class WorkspaceSettingsView(APIView):
    """`GET` is open to any member — knowing whether approval is required is
    not itself a paid fact, and a Free/Pro workspace's answer is always
    `False` by construction (nothing below Advanced can ever set it). `PATCH`
    is the write, gated to ADMIN+ on a plan that still has the feature.
    """

    def get_permissions(self) -> list[Any]:
        if self.request.method == "PATCH":
            return [IsAuthenticated(), HasFeature(APPROVAL_FEATURE)(), HasRole(Role.ADMIN)()]
        return [IsAuthenticated()]

    @extend_schema(
        responses={200: WorkspaceSettingsSerializer}, summary="Read collaboration settings"
    )
    def get(self, request: Request) -> Response:
        return Response(WorkspaceSettingsSerializer(active_workspace(request)).data)

    @extend_schema(
        request=WorkspaceSettingsSerializer,
        responses={200: WorkspaceSettingsSerializer},
        summary="Toggle whether posts require approval before scheduling",
    )
    def patch(self, request: Request) -> Response:
        workspace = active_workspace(request)
        payload = WorkspaceSettingsSerializer(workspace, data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        workspace.requires_approval = payload.validated_data["requires_approval"]
        workspace.save(update_fields=["requires_approval", "updated_at"])
        approvals.log(
            workspace=workspace,
            actor=authenticated_user(request),
            verb="workspace.requires_approval",
            meta={"value": workspace.requires_approval},
        )
        return Response(WorkspaceSettingsSerializer(workspace).data)


class AuditLogView(WorkspaceScopedQuerySetMixin, ListAPIView[AuditLog]):
    queryset = AuditLog.objects.select_related("actor")
    serializer_class = AuditLogSerializer
    permission_classes: list[Any] = [IsAuthenticated, HasFeature(APPROVAL_FEATURE)]
    pagination_class = DefaultPagination

    @extend_schema(summary="The workspace's audit trail, newest first")
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().get(request, *args, **kwargs)


class MembershipViewSet(WorkspaceScopedQuerySetMixin, viewsets.GenericViewSet[Membership]):
    """The team roster. Read by any member; add/retune/remove is ADMIN+.

    Hand-written actions rather than DRF's generic mixins: `create` and
    `partial_update` take a narrower request shape than they return (an email
    and a role in, the full roster row out), which the mixins' single
    `serializer_class` cannot express without the write shape leaking into the
    read response or vice versa.
    """

    queryset = Membership.objects.select_related("user", "invited_by", "workspace")
    serializer_class = MembershipSerializer
    pagination_class = None  # a team roster; §4.1's largest cap is 25 rows.

    def get_permissions(self) -> list[Any]:
        if self.request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            return [IsAuthenticated(), HasRole(Role.ADMIN)()]
        return [IsAuthenticated()]

    @extend_schema(responses={200: MembershipSerializer(many=True)}, summary="List the team")
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(MembershipSerializer(self.get_queryset(), many=True).data)

    @extend_schema(responses={200: MembershipSerializer}, summary="Read one team member")
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(MembershipSerializer(self.get_object()).data)

    @extend_schema(
        request=MemberAddSerializer,
        responses={201: MembershipSerializer},
        summary="Add an existing OCCS account to the team",
        description="See `workspaces.services.membership` for why this takes an existing "
        "account's email rather than sending an invitation email.",
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = MemberAddSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        added = membership.add_member(
            self.get_workspace(),
            email=payload.validated_data["email"],
            role=payload.validated_data["role"],
            invited_by=authenticated_user(request),
        )
        return Response(MembershipSerializer(added).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=MembershipRoleUpdateSerializer,
        responses={200: MembershipSerializer},
        summary="Change a member's role",
    )
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = MembershipRoleUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        updated = membership.change_role(self.get_object(), role=payload.validated_data["role"])
        return Response(MembershipSerializer(updated).data)

    @extend_schema(responses={204: None}, summary="Remove a member")
    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        membership.remove_member(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)
