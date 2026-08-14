"""Collaboration serialisation (design.md §7, §8.8; implementation.md Phase 13)."""

from __future__ import annotations

from typing import ClassVar

from rest_framework import serializers

from workspaces.models import ApprovalAction, AuditLog, Membership, PostComment, Role, Workspace


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
        return obj.user_id == obj.workspace.owner_id


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
