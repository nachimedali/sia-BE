"""Collaboration serialisation.

Shapes only. Every authority question — which threads exist for this caller,
which comments inside them — is answered by the queryset before a serializer
sees a row (P2-03).
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from accounts.models import User
from collaboration.models import (
    Annotation,
    Comment,
    Reaction,
    ReviewLink,
    ReviewLinkPurpose,
    Thread,
    ThreadStatus,
)
from common.visibility import Visibility
from common.workspaces import (
    scope_related_field_to_members,
    scope_related_field_to_workspace,
)
from content.models import Post, PostStatus
from content.serializers import AdaptedPayloadSerializer


class AnnotationSerializer(serializers.ModelSerializer[Annotation]):
    class Meta:
        model = Annotation
        fields: ClassVar[tuple[str, ...]] = (
            "anchor_start",
            "anchor_end",
            "anchor_text",
            "orphaned",
        )
        read_only_fields = fields


class ReactionSerializer(serializers.ModelSerializer[Reaction]):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Reaction
        fields: ClassVar[tuple[str, ...]] = ("id", "emoji", "user", "user_email", "created_at")
        read_only_fields = fields


@extend_schema_serializer(component_name="InternalComment")
class CommentSerializer(serializers.ModelSerializer[Comment]):
    """**Named `InternalComment` in the schema, not `Comment`.**

    `analytics.serializers.CommentSerializer` is the audience surface, and the
    two share an English word and nothing else (L-3). Left to collide, the
    generated client would carry one type where two belong — which is the merge
    the whole design forbids, arriving through the back door.
    """

    author_email = serializers.EmailField(source="author.email", read_only=True)
    annotation = AnnotationSerializer(read_only=True)
    reactions = ReactionSerializer(many=True, read_only=True)

    class Meta:
        model = Comment
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "thread",
            "author",
            "author_email",
            "body",
            "parent",
            "visibility",
            "annotation",
            "reactions",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "thread",
            "author",
            "author_email",
            "annotation",
            "reactions",
            "created_at",
            "updated_at",
        )

    def validate_parent(self, value: Comment | None) -> Comment | None:
        """A reply belongs to the thread it is posted in.

        Without this, a caller could thread a comment onto another workspace's
        conversation by guessing an id — a tenancy leak wearing a reply's
        clothes, and one the thread-level queryset scope cannot see because the
        *thread* in the URL is legitimately theirs.
        """
        thread = self.context.get("thread")
        if value is not None and thread is not None and value.thread_id != thread.pk:
            raise serializers.ValidationError("A reply must be on a comment in this thread.")
        return value


@extend_schema_serializer(component_name="InternalCommentCreate")
class CommentCreateSerializer(CommentSerializer):
    """Adds the write-only anchor. Separate from `CommentSerializer` so the
    read shape stays exactly the stored row — an anchor is an *instruction* to
    create an annotation, not a field of the comment, and modelling it as one
    would make `PATCH` look like it could move an annotation, which P2-02
    forbids."""

    anchor_start = serializers.IntegerField(write_only=True, required=False, min_value=0)
    anchor_end = serializers.IntegerField(write_only=True, required=False, min_value=1)

    class Meta(CommentSerializer.Meta):
        fields: ClassVar[tuple[str, ...]] = (
            *CommentSerializer.Meta.fields,
            "anchor_start",
            "anchor_end",
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        has_start = "anchor_start" in attrs
        if has_start != ("anchor_end" in attrs):
            raise serializers.ValidationError(
                {"anchor_end": "An anchor needs both `anchor_start` and `anchor_end`."}
            )
        return attrs


class ThreadSerializer(serializers.ModelSerializer[Thread]):
    opened_by_email = serializers.EmailField(source="opened_by.email", read_only=True, default=None)
    assignee_email = serializers.EmailField(source="assignee.email", read_only=True, default=None)
    comment_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Thread
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "title",
            "status",
            "visibility",
            "assignee",
            "assignee_email",
            "opened_by",
            "opened_by_email",
            "resolved_by",
            "resolved_at",
            "comment_count",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "opened_by",
            "opened_by_email",
            "assignee_email",
            "resolved_by",
            "resolved_at",
            "comment_count",
            "created_at",
            "updated_at",
        )


class ThreadCreateSerializer(serializers.Serializer[Any]):
    """Opening a thread writes two rows, so the request body is not the shape
    of either one.

    Both related fields are declared empty and narrowed in `__init__` — the
    established shape here (`RecurrenceRuleSerializer`): a class-level queryset
    is evaluated once per process and would be the same for every tenant, which
    is precisely the leak the two `scope_related_field_to_*` helpers close.
    """

    post = serializers.PrimaryKeyRelatedField(queryset=Post.objects.none())
    title = serializers.CharField(max_length=200)
    body = serializers.CharField()
    visibility = serializers.ChoiceField(choices=Visibility.choices, default=Visibility.INTERNAL)
    assignee = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.none(), required=False, allow_null=True
    )
    anchor_start = serializers.IntegerField(required=False, min_value=0)
    anchor_end = serializers.IntegerField(required=False, min_value=1)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        scope_related_field_to_workspace(self.fields["post"], request, Post)
        scope_related_field_to_members(self.fields["assignee"], request)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if ("anchor_start" in attrs) != ("anchor_end" in attrs):
            raise serializers.ValidationError(
                {"anchor_end": "An anchor needs both `anchor_start` and `anchor_end`."}
            )
        return attrs


class ThreadStatusRequestSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=ThreadStatus.choices)


class ThreadAssignRequestSerializer(serializers.Serializer[Any]):
    assignee = serializers.PrimaryKeyRelatedField(queryset=User.objects.none(), allow_null=True)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_members(self.fields["assignee"], self.context.get("request"))


class ReactionRequestSerializer(serializers.Serializer[Any]):
    emoji = serializers.CharField(max_length=32)


# -----------------------------------------------------------------------------
# Token-scoped review (P2-09)
# -----------------------------------------------------------------------------
class ReviewLinkSerializer(serializers.ModelSerializer[ReviewLink]):
    """**The raw token is never in here.** It exists once, in the email, so a
    leak of an API response cannot be replayed into someone else's draft — the
    same discipline `Invitation` and `Reminder` already keep."""

    is_usable = serializers.BooleanField(read_only=True)

    class Meta:
        model = ReviewLink
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "purpose",
            "email",
            "display_name",
            "expires_at",
            "used_at",
            "revoked_at",
            "is_usable",
            "created_at",
        )
        read_only_fields = fields


class ShareRequestSerializer(serializers.Serializer[Any]):
    purpose = serializers.ChoiceField(choices=ReviewLinkPurpose.choices)
    email = serializers.EmailField()
    display_name = serializers.CharField(max_length=120, required=False, allow_blank=True)


class GuestCommentSerializer(serializers.ModelSerializer[Comment]):
    """A comment as a guest sees it — signed with a name, never with whichever
    of the two author columns happens to be set."""

    author_label = serializers.CharField(read_only=True)

    class Meta:
        model = Comment
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "thread",
            "author_label",
            "body",
            "created_at",
        )
        read_only_fields = fields


class GuestThreadSerializer(serializers.ModelSerializer[Thread]):
    comments = GuestCommentSerializer(many=True, read_only=True)

    class Meta:
        model = Thread
        fields: ClassVar[tuple[str, ...]] = ("id", "title", "status", "comments", "created_at")
        read_only_fields = fields


class ReviewPostSerializer(serializers.Serializer[Any]):
    """The post as a guest sees it.

    A declared shape rather than a `SerializerMethodField` returning a dict:
    the generated client is built from this schema, and an untyped object there
    would make every field on the review page a cast — which is exactly where a
    renamed field stops being a compile error.

    **Deliberately narrower than `PostSerializer`.** A guest gets no author, no
    media ids, no delivery mode and no revision history: those are the team's,
    and a serializer that happened to include them would be a leak nobody
    noticed until a client mentioned it.
    """

    id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=PostStatus.choices)
    master_body = serializers.CharField(allow_blank=True)
    workspace_name = serializers.CharField()
    scheduled_at = serializers.DateTimeField(allow_null=True)


class ReviewPacketSerializer(serializers.Serializer[Any]):
    """What a guest reviewer is handed.

    `payloads` comes from `render_post`, **the one renderer** (Part 7 rule 1):
    a client signing off on a rendering that is not what publish sends would be
    signing off on nothing. Typed as the same `AdaptedPayload` the composer's
    preview returns, so the two surfaces cannot drift apart in the client.
    """

    post = serializers.SerializerMethodField()
    payloads = serializers.DictField(child=AdaptedPayloadSerializer(), read_only=True)
    threads = GuestThreadSerializer(many=True, read_only=True)
    can_approve = serializers.SerializerMethodField()

    @extend_schema_field(ReviewPostSerializer)
    def get_post(self, context: dict[str, Any]) -> dict[str, Any]:
        post = context["post"]
        return {
            "id": post.pk,
            "status": post.status,
            "master_body": post.master_body,
            "workspace_name": post.workspace.name,
            "scheduled_at": post.scheduled_at,
        }

    def get_can_approve(self, context: dict[str, Any]) -> bool:
        """Whether to draw the approve button.

        A hint for the UI, never the gate: `ReviewApproveView` refuses a
        `GUEST_VIEW` link with a 404 whatever this said, the same way a 402 is
        the backstop behind every entitlement-gated control.
        """
        from content.models import PostStatus

        link = context["link"]
        is_approve_link: bool = link.purpose == ReviewLinkPurpose.APPROVE
        return is_approve_link and context["post"].status == PostStatus.PENDING_REVIEW


class GuestCommentRequestSerializer(serializers.Serializer[Any]):
    body = serializers.CharField()
    thread = serializers.PrimaryKeyRelatedField(
        queryset=Thread.objects.none(), required=False, allow_null=True
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        link = self.context.get("link")
        # Scoped to the threads this guest can already see. Without it a guest
        # could post into an internal thread on their own post by guessing an
        # id — which would put their words somewhere they cannot read them back.
        field: Any = self.fields["thread"]
        field.queryset = (
            Thread.objects.filter(post=link.post, visibility=Visibility.SHARED)
            if link is not None
            else Thread.objects.none()
        )
