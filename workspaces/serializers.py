"""Collaboration serialisation (design.md §7, §8.8; implementation.md Phase 13)."""

from __future__ import annotations

import functools
import zoneinfo
from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from billing.models import OrganizationAddon
from categories.models import Category
from common.workspaces import scope_related_field_to_members
from workspaces.models import (
    PERMISSIONS,
    ApiKey,
    ApprovalAction,
    ApprovalChain,
    ApprovalStage,
    AuditLog,
    BrandVoice,
    BusinessType,
    Invitation,
    Membership,
    Organization,
    Role,
    Workspace,
    resolved_permissions,
)


class ApprovalActionSerializer(serializers.ModelSerializer[ApprovalAction]):
    #: Null on the P2-06 grandfathered rows, which have no person behind
    #: them. `default=None` rather than a placeholder string: "system" in an
    #: email column is a value a client could try to mail.
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = ApprovalAction
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "actor",
            "actor_email",
            "action",
            "stage",
            "note",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class ApprovalNoteRequestSerializer(serializers.Serializer[object]):
    """Body for `approve`/`request-changes`/`reject`. `note` is optional for
    approve/reject — a rubber stamp needs no explanation — but the view
    requires it for `request_changes`, where an empty note would leave the
    author with nothing to act on.

    `stage` is the caller's belief about where the post is (P2-08). Optional,
    and a stale value is a **409** rather than an approval of whatever stage the
    post has since moved to — the ordinary lost-update guard, which matters here
    because the thing being lost is a sign-off.
    """

    stage = serializers.IntegerField(required=False)

    note = serializers.CharField(required=False, allow_blank=True, default="")


class AuditLogSerializer(serializers.ModelSerializer[AuditLog]):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "actor_email",
            "verb",
            "target_repr",
            "meta",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class MembershipSerializer(serializers.ModelSerializer[Membership]):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    invited_by_email = serializers.EmailField(
        source="invited_by.email", read_only=True, default=None
    )
    is_owner = serializers.SerializerMethodField()
    #: What this member may do — the authority, resolved the way the gates
    #: resolve it (`member_permissions`' dual read), so a screen that hides a
    #: button and the endpoint that refuses it agree about one person.
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "user",
            "user_email",
            # The workspace joined, so a fresh accept can open it (P6).
            "workspace",
            "role",
            "permissions",
            "invited_by_email",
            "is_owner",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_permissions(self, obj: Membership) -> list[str]:
        return sorted(resolved_permissions(obj.role, obj.permissions))

    def get_is_owner(self, obj: Membership) -> bool:
        # Not the same fact as `role == OWNER`: nothing enforces the two stay
        # in sync at the schema level, so the FE renders from the field that
        # actually gates the "cannot change this row" behaviour
        # (`workspaces.services.membership`'s owner guard).
        return obj.user_id == obj.workspace.organization.owner_id


class MemberAddSerializer(serializers.Serializer[object]):
    """`POST /workspaces/members/`. Shape only — `workspaces.services.
    membership.add_member` does the account lookup, the duplicate check, the
    `max_workspace_members` quota preflight (I8) and rejects `OWNER`, so a
    402/404/409 comes back with the right envelope instead of a generic 400.

    `role` accepts the model's full `Role.choices` here rather than the
    narrower assignable subset — restricting it in the field would give this
    serializer's `role` enum a different shape from `MembershipSerializer`'s
    (the same field, model-derived), which is exactly the ambiguity
    `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`'s existing entries exist to
    avoid. One shared enum, one place (the service) that narrows it.
    """

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Role.choices)


class MembershipRoleUpdateSerializer(serializers.Serializer[object]):
    """A role (the preset) or the permissions themselves (P0-48, P5); one of the two."""

    role = serializers.ChoiceField(choices=Role.choices, required=False)
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=sorted(PERMISSIONS)), required=False, allow_empty=True
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "role" not in attrs and "permissions" not in attrs:
            raise serializers.ValidationError("Send a role or a list of permissions.")
        return attrs


class MemberRemoveSerializer(serializers.Serializer[object]):
    """Who takes over the member's open threads and scheduled posts (P5)."""

    reassign_to = serializers.IntegerField(required=False, allow_null=True)


class OwnedItemSerializer(serializers.Serializer[object]):
    kind = serializers.ChoiceField(choices=["thread", "post"])
    id = serializers.IntegerField()
    title = serializers.CharField()
    at = serializers.DateTimeField(required=False, allow_null=True)


class OwnershipSerializer(serializers.Serializer[object]):
    threads = serializers.IntegerField()
    scheduled = serializers.IntegerField()
    items = OwnedItemSerializer(many=True)


class PendingInvitationSerializer(serializers.ModelSerializer[Invitation]):
    """A pending invite on the team page (P5). Never carries the token."""

    role_label = serializers.SerializerMethodField()
    invited_by = serializers.EmailField(source="invited_by.email", default=None, read_only=True)
    account_exists = serializers.SerializerMethodField()

    class Meta:
        model = Invitation
        fields = (
            "id",
            "email",
            "role",
            "role_label",
            "invited_by",
            "account_exists",
            "created_at",
            "expires_at",
        )
        read_only_fields = fields

    def get_role_label(self, obj: Invitation) -> str:
        return str(Role(obj.role).label)

    def get_account_exists(self, obj: Invitation) -> bool:
        from workspaces.services.invitations import has_account

        annotated = getattr(obj, "has_account", None)
        return annotated if annotated is not None else has_account(obj.email)


class WorkspaceSettingsSerializer(serializers.Serializer[Any]):
    """The workspace's review posture, in the one word a settings screen uses.

    **Backed by `ApprovalChain.blocks_publish`, not by a column** (P2-04). The
    field name survives because it still reads correctly — "does this workspace
    require a separate approval before scheduling" — while the storage moved to
    the chain, which is the only thing that can also express *how many* stages
    and *who*. Keeping a boolean beside it would be two sources of truth for
    one question, and the boolean would be the one that drifted.

    `False` does **not** mean unreviewed (L-2): it means whoever schedules the
    post is the one approving it, recorded as an `APPROVE` action either way.
    """

    requires_approval = serializers.BooleanField()
    stage_count = serializers.IntegerField(read_only=True)
    max_stages = serializers.IntegerField(read_only=True)


class ApprovalStageSerializer(serializers.ModelSerializer[ApprovalStage]):
    required_approver_emails = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalStage
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "order",
            "name",
            "required_approvers",
            "required_approver_emails",
            "min_approvals",
            "allow_self_approve",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "order", "required_approver_emails")

    def get_required_approver_emails(self, stage: ApprovalStage) -> list[str]:
        return sorted(user.email for user in stage.required_approvers.all())

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_members(
            self.fields["required_approvers"], self.context.get("request")
        )


class ApprovalChainSerializer(serializers.ModelSerializer[ApprovalChain]):
    stages = ApprovalStageSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalChain
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "is_default",
            "blocks_publish",
            "stages",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "is_default",
            "stages",
            "created_at",
            "updated_at",
        )


class OrganizationSerializer(serializers.ModelSerializer[Organization]):
    """The paying company (L-1). `plan` is read-only here: a plan changes
    through billing, never through a PATCH on the org."""

    plan_code = serializers.CharField(source="plan.code", read_only=True, default="")
    workspace_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "plan_code",
            "workspace_count",
            "trial_posts_used",
            "referral_code",
            "created_at",
        )
        read_only_fields = ("id", "slug", "trial_posts_used", "referral_code", "created_at")


class WorkspaceSerializer(serializers.ModelSerializer[Workspace]):
    """A brand inside the organization.

    `status` is read-only: billing decides it (P0-18). The counts are
    annotations from `WorkspaceListCreateView.annotated`, which every response
    goes through, so a card never shows a number nobody counted.
    """

    member_count = serializers.IntegerField(read_only=True)
    product_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Workspace
        fields = (
            "id",
            "name",
            "slug",
            "status",
            "logo",
            "website",
            "description",
            "timezone",
            "soft_budget_posts",
            "soft_budget_credits",
            "member_count",
            "product_count",
            "created_at",
        )
        read_only_fields = ("id", "slug", "status", "created_at")


class ResolvedAmountSerializer(serializers.Serializer[Any]):
    amount_minor = serializers.IntegerField(read_only=True)
    currency = serializers.CharField(read_only=True)
    display = serializers.CharField(read_only=True)


class WorkspaceQuoteSerializer(serializers.Serializer[Any]):
    """What the next workspace would cost, stated before Create (P0-17).

    `included` is -1 on an unlimited plan. `next_billed` is false beyond the
    allowance when there is no subscription to bill against yet (a trial):
    the page must not promise a charge that will not happen.
    """

    included = serializers.IntegerField(read_only=True)
    used = serializers.IntegerField(read_only=True)
    extra = serializers.IntegerField(read_only=True)
    beyond_allowance = serializers.BooleanField(read_only=True)
    next_billed = serializers.BooleanField(read_only=True)
    per_workspace = ResolvedAmountSerializer(read_only=True)
    prorated = ResolvedAmountSerializer(read_only=True, allow_null=True)
    days_left = serializers.IntegerField(read_only=True, allow_null=True)
    cycle_days = serializers.IntegerField(read_only=True, allow_null=True)


class InvitationSerializer(serializers.ModelSerializer[Invitation]):
    """Never carries the token. The raw value exists once, in the email — a
    list endpoint that echoed it would make every admin's screen a credential
    store."""

    class Meta:
        model = Invitation
        fields = ("id", "email", "role", "expires_at", "accepted_at", "created_at")
        read_only_fields = fields


class InvitationCreateSerializer(serializers.Serializer[object]):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Role.choices, default=Role.VIEWER)

    def validate_role(self, value: str) -> str:
        # P0-12: ownership is `Organization.owner`, not something an invite can
        # confer.
        if value == Role.OWNER:
            raise serializers.ValidationError("OWNER is not an assignable role.")
        return value


class InvitationAcceptSerializer(serializers.Serializer[object]):
    """`password` is required only when the invited address has no account
    yet; the view decides, because only it knows."""

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    #: A new account's name (P6); ignored for an address that already has one.
    name = serializers.CharField(required=False, allow_blank=True, max_length=150)


class InvitationAcceptedSerializer(MembershipSerializer):
    """The accept's answer (P6): the membership, and the session it opens."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)

    class Meta(MembershipSerializer.Meta):
        fields: ClassVar[tuple[str, ...]] = (*MembershipSerializer.Meta.fields, "access", "refresh")


class InvitationPreviewSerializer(serializers.Serializer[object]):
    """What the invite page shows as it opens (P6): who, where, as what."""

    inviter = serializers.CharField()
    workspace_name = serializers.CharField()
    role = serializers.CharField()
    role_label = serializers.CharField()
    email = serializers.EmailField()
    account_exists = serializers.BooleanField()
    expires_at = serializers.DateTimeField()


class MembershipPermissionsSerializer(serializers.Serializer[object]):
    """P0-48: sets `permissions`, not `role`. `role` survives as a display
    preset and is derived, never dictated, once a caller starts sending
    explicit permissions."""

    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=sorted(PERMISSIONS)), allow_empty=True
    )


class OrganizationAddonSerializer(serializers.ModelSerializer[OrganizationAddon]):
    class Meta:
        model = OrganizationAddon
        fields = ("id", "addon_key", "status", "trial_ends_at", "created_at")
        read_only_fields = ("id", "created_at")


class OrganizationAddonWriteSerializer(serializers.Serializer[object]):
    addon_key = serializers.CharField(max_length=64)
    enabled = serializers.BooleanField(default=True)


class ApiKeySerializer(serializers.ModelSerializer[ApiKey]):
    class Meta:
        model = ApiKey
        fields = ("id", "name", "prefix", "scopes", "last_used_at", "revoked_at", "created_at")
        read_only_fields = fields


class ApiKeyCreateSerializer(serializers.Serializer[object]):
    name = serializers.CharField(max_length=120)
    scopes = serializers.ListField(child=serializers.ChoiceField(choices=sorted(ApiKey.SCOPES)))


class ApiKeyIssuedSerializer(serializers.Serializer[object]):
    """The one response that carries the raw key. It is never retrievable
    again — storing it would defeat hashing it."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    prefix = serializers.CharField()
    scopes = serializers.ListField(child=serializers.CharField())
    key = serializers.CharField()


@functools.lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """`available_timezones()` walks TZPATH and rebuilds its set on every call —
    ~6ms, which is not something to spend per save."""
    return frozenset(zoneinfo.available_timezones())


class WorkspaceProfileSerializer(serializers.ModelSerializer[Workspace]):
    """The brand's own description of itself: what the setup wizard collects
    and Settings edits. One set of rules for both, so a value the wizard
    accepts is never one Settings refuses."""

    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.filter(is_active=True), allow_null=True, required=False
    )
    business_type = serializers.ChoiceField(
        choices=BusinessType.choices, required=False, allow_blank=True
    )
    brand_voice_default = serializers.ChoiceField(choices=BrandVoice.choices, required=False)

    class Meta:
        model = Workspace
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "website",
            "description",
            "brand_voice_default",
            "category",
            "business_type",
            "target_audience",
            "timezone",
            "regions",
            "platforms",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id",)

    def validate_timezone(self, value: str) -> str:
        if value and value not in _known_timezones():
            raise serializers.ValidationError(f"'{value}' is not a recognised IANA timezone.")
        return value

    def validate_regions(self, value: Any) -> list[str]:
        return self._string_list("regions", value)

    def validate_platforms(self, value: Any) -> list[str]:
        return self._string_list("platforms", value)

    @staticmethod
    def _string_list(name: str, value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise serializers.ValidationError(f"{name} must be a list of strings.")
        # Deduplicate while preserving order, so a double-tapped chip does not
        # persist twice.
        seen: dict[str, None] = {}
        for item in value:
            cleaned = item.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return list(seen)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Business name cannot be blank.")
        return value.strip()
