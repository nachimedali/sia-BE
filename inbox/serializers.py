"""The inbox's shapes (S2). Read shapes are plain serializers over the queried
rows; the writes validate their own input and nothing else."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from analytics.models import AudienceComment, CommentAction
from analytics.serializers import (
    CommentCapabilitiesSerializer,
    CommentStateSerializer,
    normalised_capabilities,
)
from analytics.services.audience import state_of
from inbox.models import CommentNote, Intent, SavedReply, TriageStatus


def _person(user: Any) -> dict[str, Any] | None:
    return {"id": user.pk, "email": user.email} if user else None


class PersonSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    email = serializers.EmailField()


class InboxPostSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    title = serializers.CharField()
    permalink = serializers.CharField(allow_blank=True)


class InboxCommentSerializer(serializers.ModelSerializer[AudienceComment]):
    platform = serializers.CharField(source="post_target.platform", read_only=True)
    post = serializers.SerializerMethodField()
    intent = serializers.SerializerMethodField()
    coding_source = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    assignee = serializers.SerializerMethodField()
    note_count = serializers.IntegerField(read_only=True)
    replies = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()
    native_url = serializers.SerializerMethodField()

    class Meta:
        model = AudienceComment
        fields = (
            "id",
            "author",
            "body",
            "posted_at",
            "platform",
            "post",
            "sentiment",
            "intent",
            "coding_source",
            "status",
            "assignee",
            "note_count",
            "replies",
            "state",
            "capabilities",
            "native_url",
        )
        read_only_fields = fields

    @extend_schema_field(InboxPostSerializer)
    def get_post(self, comment: AudienceComment) -> dict[str, Any]:
        post = comment.post_target.post
        return {
            "id": post.pk,
            "title": " ".join(post.master_body.split())[:80],
            "permalink": comment.post_target.permalink,
        }

    @extend_schema_field(serializers.ChoiceField(choices=Intent.choices, allow_null=True))
    def get_intent(self, comment: AudienceComment) -> str | None:
        coding = getattr(comment, "coding", None)
        return coding.intent if coding else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_coding_source(self, comment: AudienceComment) -> str | None:
        coding = getattr(comment, "coding", None)
        return coding.source if coding else None

    @extend_schema_field(serializers.ChoiceField(choices=TriageStatus.choices))
    def get_status(self, comment: AudienceComment) -> str:
        triage = getattr(comment, "triage", None)
        return triage.status if triage else TriageStatus.OPEN

    @extend_schema_field(PersonSerializer(allow_null=True))
    def get_assignee(self, comment: AudienceComment) -> dict[str, Any] | None:
        triage = getattr(comment, "triage", None)
        return _person(triage.assignee) if triage else None

    def get_replies(self, comment: AudienceComment) -> int:
        return sum(1 for row in comment.actions.all() if row.action == CommentAction.REPLY)

    @extend_schema_field(CommentStateSerializer)
    def get_state(self, comment: AudienceComment) -> dict[str, Any]:
        return dict(CommentStateSerializer(state_of(comment)).data)

    @extend_schema_field(CommentCapabilitiesSerializer(allow_null=True))
    def get_capabilities(self, comment: AudienceComment) -> dict[str, bool] | None:
        return normalised_capabilities(comment.capabilities)

    def get_native_url(self, comment: AudienceComment) -> str:
        return str(comment.post_target.permalink or "")


class NoteSerializer(serializers.ModelSerializer[CommentNote]):
    author = serializers.SerializerMethodField()

    class Meta:
        model = CommentNote
        fields = ("id", "author", "body", "created_at")
        read_only_fields = fields

    @extend_schema_field(PersonSerializer(allow_null=True))
    def get_author(self, note: CommentNote) -> dict[str, Any] | None:
        return _person(note.author)


class ReplyRowSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    body = serializers.CharField()
    actor = PersonSerializer(allow_null=True)
    created_at = serializers.DateTimeField()


class InboxCommentDetailSerializer(InboxCommentSerializer):
    notes = serializers.SerializerMethodField()
    reply_history = serializers.SerializerMethodField()

    class Meta(InboxCommentSerializer.Meta):
        fields = (*InboxCommentSerializer.Meta.fields, "notes", "reply_history")  # type: ignore[assignment]
        read_only_fields = fields  # type: ignore[assignment]

    @extend_schema_field(NoteSerializer(many=True))
    def get_notes(self, comment: AudienceComment) -> list[dict[str, Any]]:
        return list(NoteSerializer(comment.notes.select_related("author"), many=True).data)

    @extend_schema_field(ReplyRowSerializer(many=True))
    def get_reply_history(self, comment: AudienceComment) -> list[dict[str, Any]]:
        rows = [row for row in comment.actions.all() if row.action == CommentAction.REPLY]
        return [
            {
                "id": row.pk,
                "body": row.body,
                "actor": _person(row.actor),
                "created_at": row.created_at,
            }
            for row in sorted(rows, key=lambda row: row.created_at)
        ]


class TriageUpdateSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=TriageStatus.choices, required=False)
    assignee = serializers.IntegerField(allow_null=True, required=False)
    intent = serializers.ChoiceField(choices=Intent.choices, allow_null=True, required=False)


class NoteWriteSerializer(serializers.Serializer[Any]):
    body = serializers.CharField(max_length=2000, trim_whitespace=True)


class InboxReplySerializer(serializers.Serializer[Any]):
    body = serializers.CharField(max_length=1000, trim_whitespace=True)


class PlatformCoverageSerializer(serializers.Serializer[Any]):
    platform = serializers.CharField()
    state = serializers.ChoiceField(choices=["available", "none_yet", "unavailable"])
    comments = serializers.IntegerField()


class InboxSummarySerializer(serializers.Serializer[Any]):
    total = serializers.IntegerField()
    open = serializers.IntegerField()
    done = serializers.IntegerField()
    unassigned = serializers.IntegerField()
    mine = serializers.IntegerField()
    intents = serializers.DictField(child=serializers.IntegerField())
    sentiments = serializers.DictField(child=serializers.IntegerField())
    platforms = PlatformCoverageSerializer(many=True)
    can_reply = serializers.BooleanField()


class SavedReplySerializer(serializers.ModelSerializer[SavedReply]):
    class Meta:
        model = SavedReply
        fields = ("id", "title", "body", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_title(self, value: str) -> str:
        title = value.strip()
        request = self.context["request"]
        from common.workspaces import request_workspace

        clash = SavedReply.objects.filter(workspace=request_workspace(request), title__iexact=title)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("A saved reply with that title already exists.")
        return title
