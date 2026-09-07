"""Collaboration serialisation (design.md §7, §8.8; implementation.md Phase 13)."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from billing.models import OrganizationAddon
from workspaces.models import (
    PERMISSIONS,
    ApiKey,
    ApprovalAction,
    AuditLog,
    Invitation,
    Membership,
    Organization,
    PostComment,
    Role,
    Workspace,
)


class ApprovalActionSerializer(serializers.ModelSerializer[ApprovalAction]):
    actor_email = serializers.EmailField(source="actor.email", read_only=True)

    class Meta:
        model = ApprovalAction
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "actor",
            "actor_email",
            "action",
            "note",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class ApprovalNoteRequestSerializer(serializers.Serializer[object]):
    """Body for `approve`/`request-changes`/`reject`. `note` is optional for
    approve/reject — a rubber stamp needs no explanation — but the view
    requires it for `request_changes`, where an empty note would leave the
    author with nothing to act on."""

    note = serializers.CharField(required=False, allow_blank=True, default="")


class PostCommentSerializer(serializers.ModelSerializer[PostComment]):
    author_email = serializers.EmailField(source="author.email", read_only=True)

    class Meta:
        model = PostComment
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "author",
            "author_email",
            "body",
            "parent",
            "resolved_at",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "author",
            "author_email",
            "resolved_at",
            "created_at",
        )

    def validate_parent(self, value: PostComment | None) -> PostComment | None:
        # A reply's parent has to be a comment on the *same* post — otherwise
        # a client could thread a new comment onto another post's thread by
        # guessing an id, which is a workspace-tenancy leak in miniature.
        post = self.context.get("post")
        if value is not None and post is not None and value.post_id != post.pk:
            raise serializers.ValidationError("A reply must be on a comment from this post.")
        return value


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

    class Meta:
        model = Membership
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "user",
            "user_email",
            "role",
            "invited_by_email",
            "is_owner",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields

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
    role = serializers.ChoiceField(choices=Role.choices)


class WorkspaceSettingsSerializer(serializers.ModelSerializer[Workspace]):
    class Meta:
        model = Workspace
        fields: ClassVar[tuple[str, ...]] = ("requires_approval",)


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
    """A brand inside the organization."""

    class Meta:
        model = Workspace
        fields = (
            "id",
            "name",
            "slug",
            "logo",
            "website",
            "description",
            "timezone",
            "soft_budget_posts",
            "soft_budget_credits",
            "created_at",
        )
        read_only_fields = ("id", "slug", "created_at")


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
