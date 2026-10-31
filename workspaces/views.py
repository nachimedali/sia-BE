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
from django.contrib.auth import get_user_model
from django.db.models import Count, Exists, OuterRef
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import issue_session
from billing.models import OrganizationAddon
from billing.permissions import HasFeature
from billing.services import subscriptions
from billing.services.entitlements import entitlements_for
from common.exceptions import NotFoundError, OCCSError, StateConflict
from common.mixins import WorkspaceScopedQuerySetMixin
from common.pagination import DefaultPagination
from common.throttling import IPTokenBucketThrottle
from common.workspaces import authenticated_user, request_workspace
from workspaces.models import (
    ApiKey,
    AuditLog,
    Invitation,
    Membership,
    Organization,
    Permission,
    Role,
    Workspace,
    resolved_permissions,
)
from workspaces.permissions import HasPermission
from workspaces.serializers import (
    ApiKeyCreateSerializer,
    ApiKeyIssuedSerializer,
    ApiKeySerializer,
    ApprovalChainSerializer,
    ApprovalStageSerializer,
    AuditLogSerializer,
    InvitationAcceptedSerializer,
    InvitationAcceptSerializer,
    InvitationCreateSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    MemberAddSerializer,
    MemberRemoveSerializer,
    MembershipRoleUpdateSerializer,
    MembershipSerializer,
    OrganizationAddonSerializer,
    OrganizationAddonWriteSerializer,
    OrganizationSerializer,
    OwnershipSerializer,
    PendingInvitationSerializer,
    WorkspaceProfileSerializer,
    WorkspaceQuoteSerializer,
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


class WorkspaceProfileView(APIView):
    """The brand's profile, as Settings edits it. `GET` is any member's;
    `PATCH` needs `admin` — this is the text every generation is grounded in,
    so changing it is a workspace decision, not an editor's. Each change is
    written to the audit log with the fields it touched."""

    def get_permissions(self) -> list[Any]:
        if self.request.method == "PATCH":
            return [IsAuthenticated(), HasPermission(Permission.ADMIN)()]
        return [IsAuthenticated(), HasPermission(Permission.VIEW)()]

    @extend_schema(responses={200: WorkspaceProfileSerializer}, summary="Read the brand profile")
    def get(self, request: Request) -> Response:
        return Response(WorkspaceProfileSerializer(request_workspace(request)).data)

    @extend_schema(
        request=WorkspaceProfileSerializer,
        responses={200: WorkspaceProfileSerializer},
        summary="Update the brand profile",
        description="Partial: send only the fields that changed.",
    )
    def patch(self, request: Request) -> Response:
        workspace = request_workspace(request)
        serializer = WorkspaceProfileSerializer(workspace, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # Compared on the stored column, so a foreign key (category) is
        # compared by id rather than loaded to be compared.
        def stored(field: str) -> Any:
            column = f"{field}_id"
            return (
                getattr(workspace, column)
                if hasattr(workspace, column)
                else getattr(workspace, field)
            )

        changed = sorted(
            field
            for field, value in serializer.validated_data.items()
            if stored(field) != (value.pk if hasattr(value, "pk") else value)
        )
        serializer.save()
        if changed:
            approvals.log(
                workspace=workspace,
                actor=authenticated_user(request),
                verb="workspace.profile_updated",
                meta={"fields": changed},
            )
        return Response(serializer.data)


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

        approvers = payload.validated_data.pop("required_approvers", [])
        # Locked and depth-checked together, inside `append_stage` (found in
        # review: the old unlocked read-then-create here could race two
        # concurrent appends onto the same `order` and crash with a raw 500).
        approvals.append_stage(chain, required_approvers=approvers, **payload.validated_data)
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

    queryset = Membership.objects.select_related("user", "invited_by", "workspace__organization")
    serializer_class = MembershipSerializer
    pagination_class = None  # a team roster; §4.1's largest cap is 25 rows.

    def get_permissions(self) -> list[Any]:
        if self.request.method in {"POST", "PATCH", "PUT", "DELETE"} or self.action == "ownership":
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
        summary="Add an existing ComposeVision account to the team",
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
        updated = self.get_object()
        if "role" in payload.validated_data:
            updated = membership.change_role(updated, role=payload.validated_data["role"])
        if "permissions" in payload.validated_data:
            updated = membership.set_permissions(
                updated, permissions=payload.validated_data["permissions"]
            )
        return Response(MembershipSerializer(updated).data)

    @extend_schema(
        request=MemberRemoveSerializer,
        responses={204: None},
        summary="Remove a member, handing over their open work",
        description="409 `owns_items` when they still own open threads or scheduled posts and no "
        "`reassign_to` member is named (P5).",
    )
    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        payload = MemberRemoveSerializer(data=request.data or {})
        payload.is_valid(raise_exception=True)
        target = self.get_object()
        heir_id = payload.validated_data.get("reassign_to")
        heir = None
        if heir_id is not None:
            # Looked up without the workspace scope on purpose: a member of
            # another workspace is a 400 here (`invalid_reassign`), not a 404 —
            # the id is a payload field, not the resource being addressed.
            heir = Membership.objects.filter(pk=heir_id).select_related("user").first()
            if heir is None:
                raise membership.InvalidReassignError(detail={"reassign_to": heir_id})
        membership.remove_member(target, reassign_to=heir, actor=authenticated_user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(responses={200: OwnershipSerializer}, summary="What a member still owns")
    @action(detail=True, methods=["get"])
    def ownership(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(OwnershipSerializer(membership.ownership(self.get_object())).data)


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

    @staticmethod
    def annotated(queryset: Any) -> Any:
        """The counts each card shows. `distinct=True` on both: two joins in
        one query would otherwise multiply each other."""
        return queryset.annotate(
            member_count=Count("memberships", distinct=True),
            product_count=Count("products", distinct=True),
        ).order_by("created_at", "id")

    @extend_schema(
        responses={200: WorkspaceSerializer(many=True)}, summary="Workspaces this user can open"
    )
    def get(self, request: Request) -> Response:
        user = authenticated_user(request)
        # A subquery, not `filter(memberships__user=…)`: a filter on the same
        # relation before `annotate` would count only the caller's membership.
        mine = Membership.objects.filter(user=user).values("workspace")
        workspaces = self.annotated(Workspace.objects.filter(pk__in=mine))
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
            user=authenticated_user(request),
            name=payload.validated_data["name"],
            description=payload.validated_data.get("description", ""),
        )
        # Outside the provisioning transaction on purpose: the gateway call is
        # a network round-trip, and holding a transaction across one is how a
        # slow vendor becomes a database outage. A failure leaves the workspace
        # `PENDING_BILLING` and read-only rather than refusing it (P0-18).
        subscriptions.sync_workspace_quantity(workspace.organization)
        workspace = self.annotated(Workspace.objects.filter(pk=workspace.pk)).get()
        return Response(WorkspaceSerializer(workspace).data, status=status.HTTP_201_CREATED)


class WorkspaceQuoteView(APIView):
    """`GET /workspaces/quote/` — what the next workspace would cost (P0-17).

    For the caller's organization, the same one `provision_extra_workspace`
    creates into, so the quote and the charge are about the same company.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: WorkspaceQuoteSerializer}, summary="Price of one more workspace")
    def get(self, request: Request) -> Response:
        user = authenticated_user(request)
        organization = (
            Organization.objects.filter(memberships__user=user)
            .select_related("plan")
            .order_by("created_at")
            .first()
        )
        if organization is None:
            raise NotFoundError("No organization.", code="organization_not_found")
        quote = subscriptions.workspace_quote(organization)
        return Response(WorkspaceQuoteSerializer(quote).data)


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
        workspace = _admin_workspace(request, pk, "invite people to this workspace")

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


def _admin_workspace(
    request: Request, pk: int, action: str = "manage this workspace's invitations"
) -> Workspace:
    """The workspace `pk` if the caller is a member with `admin`: another
    workspace is 404 (Part 7 rule 3), a member without `admin` 403."""
    caller = (
        Membership.objects.select_related("workspace")
        .filter(user=authenticated_user(request), workspace_id=pk)
        .first()
    )
    if caller is None:
        raise NotFoundError("No such workspace.", code="workspace_not_found")
    if "admin" not in resolved_permissions(caller.role, caller.permissions):
        raise PermissionDenied(f"Only an admin can {action}.")
    return caller.workspace


def _pending_invite(request: Request, pk: int, invite_id: int) -> tuple[Workspace, Invitation]:
    """A pending invite of an admin's workspace, or 404."""
    workspace = _admin_workspace(request, pk)
    invitation = _pending(workspace).filter(pk=invite_id).first()
    if invitation is None:
        raise NotFoundError("No such invitation.", code="invitation_not_found")
    return workspace, invitation


def _pending(workspace: Workspace) -> Any:
    return (
        Invitation.objects.filter(
            workspace=workspace, accepted_at__isnull=True, revoked_at__isnull=True
        )
        .select_related("invited_by")
        # One query for the whole list, not one per row (read by the serializer).
        .annotate(
            has_account=Exists(get_user_model().objects.filter(email__iexact=OuterRef("email")))
        )
        .order_by("-created_at")
    )


class WorkspaceInviteListView(APIView):
    """`GET /workspaces/{id}/invites/` (P5): the invites still waiting."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: PendingInvitationSerializer(many=True)}, summary="Pending invitations"
    )
    def get(self, request: Request, pk: int) -> Response:
        workspace = _admin_workspace(request, pk)
        return Response(PendingInvitationSerializer(_pending(workspace), many=True).data)


class WorkspaceInviteDetailView(APIView):
    """`DELETE /workspaces/{id}/invites/{invite_id}/` (P5): revoke."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={204: None}, summary="Revoke a pending invitation")
    def delete(self, request: Request, pk: int, invite_id: int) -> Response:
        _workspace, invitation = _pending_invite(request, pk, invite_id)
        invitations.revoke(invitation, actor=authenticated_user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceInviteResendView(APIView):
    """`POST /workspaces/{id}/invites/{invite_id}/resend/` (P5): a fresh link
    and a fresh clock; the old link stops working."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None, responses={200: PendingInvitationSerializer}, summary="Resend an invitation"
    )
    def post(self, request: Request, pk: int, invite_id: int) -> Response:
        workspace, invitation = _pending_invite(request, pk, invite_id)
        fresh = invitations.invite(
            workspace=workspace,
            email=invitation.email,
            role=invitation.role,
            invited_by=authenticated_user(request),
            base_url=settings.SITE_URL,
        )
        return Response(PendingInvitationSerializer(fresh).data)


class InvitationGoneError(OCCSError):
    """410: the invite existed and can't be used any more (expired or used)."""

    status_code = status.HTTP_410_GONE
    default_code = "gone"


class InviteTokenThrottle(IPTokenBucketThrottle):
    """Generous, like the review one: a person retyping a password must never
    see it. Defence in depth against naive enumeration and password guessing
    through the accept form, which a 256-bit token alone does not cover."""

    scope = "workspaces:invite"
    capacity = 30
    refill_per_second = 0.5


class InvitationPreviewView(APIView):
    """`GET /invites/{token}/` (P6): checked as the page opens.

    200 with who invited you, to which workspace and with which role; 410 with
    `expired` or `used`; 404 `invalid_token` for one that never existed or was
    withdrawn.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [InviteTokenThrottle]

    @extend_schema(
        responses={200: InvitationPreviewSerializer},
        summary="Preview a workspace invitation",
        auth=[],
    )
    def get(self, request: Request, token: str) -> Response:
        invitation, state = invitations.preview(token)
        if invitation is None:
            raise NotFoundError("This invitation link is not valid.", code="invalid_token")
        if state != "ok":
            message = (
                "This invitation has expired."
                if state == "expired"
                else "This invitation was already used."
            )
            raise InvitationGoneError(message, code=state)
        return Response(
            InvitationPreviewSerializer(
                {
                    "inviter": invitations.inviter_name(invitation),
                    "workspace_name": invitation.workspace.name,
                    "role": invitation.role,
                    "role_label": Role(invitation.role).label,
                    "email": invitation.email,
                    "account_exists": invitations.has_account(invitation.email),
                    "expires_at": invitation.expires_at,
                }
            ).data
        )


class InvitationAcceptView(APIView):
    """`POST /invites/{token}/accept/` (P0-47).

    Unauthenticated: the whole point is that the invitee may have no account
    yet. The token is the credential, and it is single-use, expiring and
    stored only as a hash.
    """

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [InviteTokenThrottle]

    @extend_schema(
        request=InvitationAcceptSerializer,
        responses={201: InvitationAcceptedSerializer},
        summary="Accept a workspace invitation",
        description="Answers with the membership and a session pair, as registration does.",
    )
    def post(self, request: Request, token: str) -> Response:
        payload = InvitationAcceptSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            membership_row = invitations.accept(
                raw_token=token,
                password=payload.validated_data.get("password") or None,
                name=payload.validated_data.get("name", ""),
            )
        except invitations.InvitationNeedsPasswordError as exc:
            raise OCCSError(str(exc), code="password_required") from exc
        except invitations.InvitationWrongPasswordError as exc:
            raise OCCSError(str(exc), code="wrong_password") from exc
        except invitations.InvitationWeakPasswordError as exc:
            raise OCCSError(
                " ".join(exc.messages),
                code="invalid",
                detail={"fields": {"password": exc.messages}},
            ) from exc
        except invitations.InvitationInvalidError as exc:
            # 404, not 400: an invalid token and a token that never existed
            # are the same answer, and distinguishing them tells an attacker
            # which guesses were close.
            raise NotFoundError(
                "This invitation is no longer valid.", code="invalid_token"
            ) from exc
        session = issue_session(membership_row.user)
        return Response(
            {
                **MembershipSerializer(membership_row).data,
                "access": session["access"],
                "refresh": session["refresh"],
            },
            status=status.HTTP_201_CREATED,
        )


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

        if payload.validated_data["addon_key"] in addons.PURCHASED_ONLY:
            raise ValidationError({"addon_key": "This add-on is bought as a pack."})
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
