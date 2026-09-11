"""Collaboration endpoints (design.md §7, §8.8; implementation.md Phase 13).

`MembershipViewSet` is router-registered (`workspaces/members`) because it
returns objects by pk — the Phase 4 tenancy sweep (design.md A52) has to be
able to walk it. `WorkspaceSettingsView` and `AuditLogView` are plain paths,
the same shape `analytics/views.py` uses for reads that answer for the
caller's own workspace rather than an id in the URL.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import Count, Max
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import OrganizationAddon
from billing.permissions import HasFeature
from billing.services import subscriptions
from billing.services.entitlements import entitlements_for
from common.exceptions import NotFoundError, OCCSError, StateConflict
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.workspaces import authenticated_user, request_workspace
from workspaces.models import (
    ApiKey,
    ApprovalStage,
    AuditLog,
    Membership,
    Organization,
    Permission,
    Workspace,
    permissions_for,
)
from workspaces.permissions import HasPermission
from workspaces.serializers import (
    ApiKeyCreateSerializer,
    ApiKeyIssuedSerializer,
    ApiKeySerializer,
    ApprovalChainSerializer,
    ApprovalStageSerializer,
    AuditLogSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationSerializer,
    MemberAddSerializer,
    MembershipRoleUpdateSerializer,
    MembershipSerializer,
    OrganizationAddonSerializer,
    OrganizationAddonWriteSerializer,
    OrganizationSerializer,
    WorkspaceSerializer,
    WorkspaceSettingsSerializer,
)
from workspaces.services import (
    addons,
    approvals,
    invitations,
    membership,
    provisioning,
)

#: The workspace-wide **audit log** only. C-02 made approval itself universal,
#: so this no longer gates the approval endpoints; what it still gates is the
#: governance read beside them, which is a different purchase.
APPROVAL_FEATURE = "approval_workflow"


def _settings_payload(workspace: Workspace) -> dict[str, Any]:
    chain = approvals.default_chain(workspace)
    return {
        "requires_approval": chain.blocks_publish,
        "stage_count": chain.stages.count(),
        "max_stages": entitlements_for(workspace).max_approval_stages(),
    }


class WorkspaceSettingsView(APIView):
    """`GET` is open to any member; `PATCH` needs `admin`.

    **No plan gate on the write any more** (C-02). Requiring a separate
    reviewer is available on every plan, because approval is: what Advanced
    buys is *depth* — more than one stage — which `ApprovalChainStageView`
    enforces, not this toggle.
    """

    def get_permissions(self) -> list[Any]:
        if self.request.method == "PATCH":
            return [IsAuthenticated(), HasPermission(Permission.ADMIN)()]
        return [IsAuthenticated()]

    @extend_schema(
        responses={200: WorkspaceSettingsSerializer}, summary="Read collaboration settings"
    )
    def get(self, request: Request) -> Response:
        return Response(_settings_payload(request_workspace(request)))

    @extend_schema(
        request=WorkspaceSettingsSerializer,
        responses={200: WorkspaceSettingsSerializer},
        summary="Choose whether a separate reviewer must approve before scheduling",
        description=(
            "`false` does not mean unreviewed (L-2). It means whoever schedules "
            "the post is the one approving it, and an `APPROVE` action is "
            "recorded naming them either way."
        ),
    )
    def patch(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = WorkspaceSettingsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        chain = approvals.default_chain(workspace)
        chain.blocks_publish = payload.validated_data["requires_approval"]
        chain.save(update_fields=["blocks_publish", "updated_at"])
        approvals.log(
            workspace=workspace,
            actor=authenticated_user(request),
            verb="workspace.requires_approval",
            meta={"value": chain.blocks_publish},
        )
        return Response(_settings_payload(workspace))


class ApprovalChainStageView(APIView):
    """The stages of this workspace's default chain (P2-07, P2-13).

    A plain path rather than a router registration: it answers for the caller's
    own workspace rather than an id in the URL, so there is nothing here for the
    tenancy sweep (A52) to walk — the same shape `WorkspaceSettingsView` above
    already has.

    **Depth is the paid axis.** Adding a stage beyond `approval_chain_depth` is
    a 402 with an upgrade payload; the first stage never is.
    """

    permission_classes: list[Any] = [IsAuthenticated, HasPermission(Permission.ADMIN)]

    @extend_schema(
        responses={200: ApprovalChainSerializer}, summary="Read the default approval chain"
    )
    def get(self, request: Request) -> Response:
        chain = approvals.default_chain(request_workspace(request))
        return Response(ApprovalChainSerializer(chain, context={"request": request}).data)

    @extend_schema(
        request=ApprovalStageSerializer,
        responses={201: ApprovalChainSerializer},
        summary="Append a stage to the default approval chain",
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        chain = approvals.default_chain(workspace)
        payload = ApprovalStageSerializer(data=request.data, context={"request": request})
        payload.is_valid(raise_exception=True)

        entitlements_for(workspace).require_chain_depth(chain.stages.count())

        approvers = payload.validated_data.pop("required_approvers", [])
        stage = ApprovalStage.objects.create(
            chain=chain,
            order=(chain.stages.aggregate(Max("order"))["order__max"] or 0) + 1,
            **payload.validated_data,
        )
        stage.required_approvers.set(approvers)
        return Response(
            ApprovalChainSerializer(chain, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        responses={200: ApprovalChainSerializer},
        summary="Remove the last stage from the default approval chain",
        description=(
            "Only the last stage, and only when no post is sitting on it — "
            "removing a stage under a post in review would leave it approved by "
            "a step that no longer exists."
        ),
    )
    def delete(self, request: Request) -> Response:
        workspace = request_workspace(request)
        chain = approvals.default_chain(workspace)
        stage = chain.stages.order_by("-order").first()
        if stage is None:
            raise NotFoundError("This chain has no stages.", code="no_stage_to_remove")
        if stage.posts_in_review.exists():
            raise StateConflict(
                "A post is waiting at this stage.",
                detail={"stage": stage.pk},
                code="stage_in_use",
            )
        stage.delete()
        return Response(ApprovalChainSerializer(chain, context={"request": request}).data)


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
            return [IsAuthenticated(), HasPermission(Permission.ADMIN)()]
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


class OrganizationListView(APIView):
    """`GET /organizations/` — the paying companies this user belongs to
    (P0-46).

    A list rather than a singleton because an agency lead genuinely sits in
    several. Scoped to the caller's own memberships, so there is no id to leak
    and no cross-tenant read to defend.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: OrganizationSerializer(many=True)},
        summary="Organizations this user belongs to",
    )
    def get(self, request: Request) -> Response:
        user = authenticated_user(request)
        organizations = (
            Organization.objects.filter(memberships__user=user)
            .select_related("plan")
            .annotate(workspace_count=Count("workspaces"))
            .distinct()
        )
        return Response(OrganizationSerializer(organizations, many=True).data)


class WorkspaceListCreateView(APIView):
    """`GET/POST /workspaces/` (P0-46).

    Creation increments the organization's Stripe quantity. If that call
    fails, the workspace is still created — in `PENDING_BILLING`, read-only —
    rather than refused: the webhook is the source of truth for what was
    granted, and a workspace the customer asked for and cannot see is worse
    than one they can see and cannot yet write to (P0-18).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: WorkspaceSerializer(many=True)}, summary="Workspaces this user can open"
    )
    def get(self, request: Request) -> Response:
        user = authenticated_user(request)
        workspaces = Workspace.objects.filter(memberships__user=user).distinct()
        return Response(WorkspaceSerializer(workspaces, many=True).data)

    @extend_schema(
        request=WorkspaceSerializer,
        responses={201: WorkspaceSerializer},
        summary="Create a workspace in the caller's organization",
    )
    def post(self, request: Request) -> Response:
        payload = WorkspaceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = provisioning.provision_extra_workspace(
            user=authenticated_user(request), name=payload.validated_data["name"]
        )
        # Outside the provisioning transaction on purpose: the gateway call is
        # a network round-trip, and holding a transaction across one is how a
        # slow vendor becomes a database outage. A failure leaves the workspace
        # `PENDING_BILLING` and read-only rather than refusing it (P0-18).
        subscriptions.sync_workspace_quantity(workspace.organization)
        workspace.refresh_from_db()
        return Response(WorkspaceSerializer(workspace).data, status=status.HTTP_201_CREATED)


class WorkspaceInviteView(APIView):
    """`POST /workspaces/{id}/invite/` (P0-47).

    Requires `admin` **in the target workspace**, resolved through the
    caller's own memberships — so inviting into a workspace the caller does
    not belong to is a 404, not a 403 (Part 7 rule 3).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=InvitationCreateSerializer,
        responses={201: InvitationSerializer},
        summary="Invite someone to a workspace",
    )
    def post(self, request: Request, pk: int) -> Response:
        user = authenticated_user(request)
        workspace = Workspace.objects.filter(pk=pk, memberships__user=user).first()
        if workspace is None:
            raise NotFoundError("No such workspace.", code="workspace_not_found")

        caller = Membership.objects.filter(user=user, workspace=workspace).first()
        held = set(caller.permissions) if caller and caller.permissions else set()
        if not held and caller is not None:
            held = permissions_for(caller.role)
        if "admin" not in held:
            raise PermissionDenied("Only an admin can invite people to this workspace.")

        payload = InvitationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        invitation = invitations.invite(
            workspace=workspace,
            email=payload.validated_data["email"],
            role=payload.validated_data["role"],
            invited_by=user,
            base_url=settings.SITE_URL,
        )
        return Response(InvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)


class InvitationAcceptView(APIView):
    """`POST /invites/{token}/accept/` (P0-47).

    Unauthenticated: the whole point is that the invitee may have no account
    yet. The token is the credential, and it is single-use, expiring and
    stored only as a hash.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(
        request=InvitationAcceptSerializer,
        responses={201: MembershipSerializer},
        summary="Accept a workspace invitation",
    )
    def post(self, request: Request, token: str) -> Response:
        payload = InvitationAcceptSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            membership_row = invitations.accept(
                raw_token=token, password=payload.validated_data.get("password") or None
            )
        except invitations.InvitationNeedsPasswordError as exc:
            raise OCCSError(str(exc), code="password_required") from exc
        except invitations.InvitationInvalidError as exc:
            # 404, not 400: an invalid token and a token that never existed
            # are the same answer, and distinguishing them tells an attacker
            # which guesses were close.
            raise NotFoundError(
                "This invitation is no longer valid.", code="invalid_token"
            ) from exc
        return Response(MembershipSerializer(membership_row).data, status=status.HTTP_201_CREATED)


class OrganizationAddonView(APIView):
    """`POST /billing/addons/` — enable or disable an org add-on (P0-49)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=OrganizationAddonWriteSerializer,
        responses={200: OrganizationAddonSerializer(many=True)},
        summary="Enable or disable an organization add-on",
    )
    def post(self, request: Request) -> Response:
        payload = OrganizationAddonWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        organization = request_workspace(request).organization
        if organization is None:
            raise NotFoundError("This workspace has no organization.", code="no_organization")

        addons.set_addon(
            organization,
            key=payload.validated_data["addon_key"],
            enabled=payload.validated_data["enabled"],
        )
        rows = OrganizationAddon.objects.filter(organization=organization)
        return Response(OrganizationAddonSerializer(rows, many=True).data)


class ApiKeyView(APIView):
    """`GET/POST /api-keys/` (P0-50).

    Ships in Phase 0 with no key issued to anyone until Phase 10, because
    scopes are a versioning decision: an API released unscoped can only be
    scoped later by breaking every integration built against it.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: ApiKeySerializer(many=True)}, summary="List API keys")
    def get(self, request: Request) -> Response:
        organization = request_workspace(request).organization
        keys = ApiKey.objects.filter(organization=organization)
        return Response(ApiKeySerializer(keys, many=True).data)

    @extend_schema(
        request=ApiKeyCreateSerializer,
        responses={201: ApiKeyIssuedSerializer},
        summary="Issue an API key",
    )
    def post(self, request: Request) -> Response:
        payload = ApiKeyCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        organization = request_workspace(request).organization
        if organization is None:
            raise NotFoundError("This workspace has no organization.", code="no_organization")

        key, raw = ApiKey.issue(
            organization=organization,
            name=payload.validated_data["name"],
            scopes=payload.validated_data["scopes"],
            created_by=authenticated_user(request),
        )
        # The only response that carries the raw value. It is not retrievable
        # again — storing it would defeat hashing it.
        return Response(
            {
                "id": key.pk,
                "name": key.name,
                "prefix": key.prefix,
                "scopes": key.scopes,
                "key": raw,
            },
            status=status.HTTP_201_CREATED,
        )
