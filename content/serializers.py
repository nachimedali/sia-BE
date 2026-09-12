"""Content serialisation (design.md §7).

`PostSerializer`'s write surface is deliberately narrow (design.md A49):
`status`, `delivery_mode`, `scheduled_at`, `source` and `origin_post` are all
system-controlled until the phases that own their transitions — scheduling
(Phase 8), publishing (Phase 9), repurposing (Phase 11) — land.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.utils import timezone
from rest_framework import serializers
from rest_framework.request import Request

from common.workspaces import request_workspace, scope_related_field_to_workspace
from content.models import (
    ALT_TEXT_MAX_LENGTH,
    DeliveryMode,
    MediaAsset,
    Platform,
    Post,
    PostMediaAttachment,
    PostRevision,
    PostTemplate,
    RecurrenceRule,
)
from content.services import recurrence, templates


def _scope_media_field(field: Any, request: Request | None) -> None:
    """Restricts a `media_asset_ids` field's choices to the caller's own
    workspace — a bare model-wide queryset would let one workspace reference
    another's media."""
    scope_related_field_to_workspace(field, request, MediaAsset)


class MediaAssetSerializer(serializers.ModelSerializer[MediaAsset]):
    url = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "kind",
            "url",
            "mime",
            "width",
            "height",
            "duration_ms",
            "checksum",
            "source",
            "derived_from",
            "created_at",
        )
        read_only_fields = fields

    def get_url(self, obj: MediaAsset) -> str | None:
        return obj.file.url if obj.file else None


class MediaAssetUploadSerializer(serializers.Serializer[Any]):
    """Documents the multipart upload shape for the schema. The view reads
    `request.FILES` directly — a `FileField` here has nowhere to attach the
    workspace before `ingest_media` needs it."""

    file = serializers.FileField()


class PostMediaSerializer(serializers.ModelSerializer[PostMediaAttachment]):
    """Nested, ordered view of a post's attached media
    (`Post.ordered_attachments`).

    Serialises the **attachment**, not the asset, because alt text is a
    property of this use of the file (P1-06). `id` stays the asset's id — that
    is what every caller references media by, and what `media_asset_ids`
    writes back.
    """

    id = serializers.IntegerField(source="media_asset.id", read_only=True)
    kind = serializers.CharField(source="media_asset.kind", read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = PostMediaAttachment
        fields: ClassVar[tuple[str, ...]] = ("id", "kind", "url", "alt_text")
        read_only_fields = fields

    def get_url(self, obj: PostMediaAttachment) -> str | None:
        asset = obj.media_asset
        return asset.file.url if asset.file else None


class AltTextRequestSerializer(serializers.Serializer[Any]):
    """`POST /posts/{id}/alt-text/`. `platform` is optional: absent describes
    the image for the whole post, present describes it for that platform
    only."""

    media_asset = serializers.PrimaryKeyRelatedField(queryset=MediaAsset.objects.none())
    alt_text = serializers.CharField(
        allow_blank=True, max_length=ALT_TEXT_MAX_LENGTH, trim_whitespace=True
    )
    platform = serializers.ChoiceField(choices=Platform.choices, required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _scope_media_field(self.fields["media_asset"], self.context.get("request"))


class PlatformOptionsRequestSerializer(serializers.Serializer[Any]):
    """`POST /posts/{id}/platform-options/`. The values themselves are checked
    by `content.services.options`, which reads the declaration in `rules.py` —
    a second copy of the rules here is a second copy to drift."""

    platform = serializers.ChoiceField(choices=Platform.choices)
    options = serializers.DictField(required=False, default=dict)


class PostSerializer(serializers.ModelSerializer[Post]):
    media = PostMediaSerializer(source="ordered_attachments", many=True, read_only=True)
    media_asset_ids = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.none(), many=True, write_only=True, required=False
    )
    # The review queue's rows are "who is asking me to approve what", so the
    # author travels with the post rather than the reviewer resolving each one
    # against the roster. Same `<relation>_email` shape the collaboration
    # serializers already use for actors and comment authors.
    author_email = serializers.EmailField(source="author.email", read_only=True)

    class Meta:
        model = Post
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "content_kind",
            "doc_body",
            "master_body",
            "media",
            "media_asset_ids",
            "author_email",
            "status",
            "visibility",
            "delivery_mode",
            "scheduled_at",
            "source",
            "category",
            "origin_post",
            # The review surface needs all three to render a chain honestly:
            # which stage the post is waiting at, whether it is frozen, and the
            # time the author proposed for it (P2-07, P2-10, P2-11).
            "current_stage",
            "locked_at",
            "proposed_delivery_mode",
            "proposed_scheduled_at",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "author_email",
            "status",
            "delivery_mode",
            "scheduled_at",
            "source",
            "origin_post",
            # Written by the approval service alone, for the same reason
            # `scheduled_at` is written by the schedule service alone (A49):
            # a client that could set them would be a second, ungated path
            # into the state machine.
            "current_stage",
            "locked_at",
            "proposed_delivery_mode",
            "proposed_scheduled_at",
            "created_at",
            "updated_at",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _scope_media_field(self.fields["media_asset_ids"], self.context.get("request"))


class PostPreviewRequestSerializer(serializers.Serializer[Any]):
    master_body = serializers.CharField(allow_blank=True, default="")
    media_asset_ids = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.none(), many=True, required=False, default=list
    )
    platforms = serializers.ListField(
        child=serializers.ChoiceField(choices=Platform.choices), required=False
    )
    #: media id → description, for the composer's unsaved preview. Without it
    #: an unsaved draft would preview alt-less while the saved post publishes
    #: with descriptions — a divergence the user sees even though both sides
    #: go through `render_post` (P1-06).
    alt_text = serializers.DictField(
        child=serializers.CharField(allow_blank=True, max_length=ALT_TEXT_MAX_LENGTH),
        required=False,
        default=dict,
    )
    #: platform → stored options, for the same reason `alt_text` is here: an
    #: unsaved draft must preview the settings it is about to be saved with,
    #: not the platform defaults.
    platform_options = serializers.DictField(
        child=serializers.DictField(), required=False, default=dict
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _scope_media_field(self.fields["media_asset_ids"], self.context.get("request"))


class PostRevisionSerializer(serializers.ModelSerializer[PostRevision]):
    """History, newest first. The `snapshot` is deliberately **not** exposed:
    a version list wants "what changed and who changed it", and shipping a full
    copy of every checkpoint would make the response grow with the post rather
    than with the history."""

    author_email = serializers.EmailField(source="author.email", read_only=True, default=None)

    class Meta:
        model = PostRevision
        fields: ClassVar[tuple[str, ...]] = (
            "sequence",
            "author_email",
            "diff",
            "is_checkpoint",
            "reason",
            "created_at",
        )
        read_only_fields = fields


class PostScheduleRequestSerializer(serializers.Serializer[Any]):
    """`POST /posts/{id}/schedule/` (implementation.md Phase 8). Shape only —
    the horizon check against `Plan.scheduling_horizon_days` is an
    entitlement, not a validation rule, so it lives in
    `scheduling.services.schedule_post`, not here (design.md A2: 402 is
    reserved for entitlement failures)."""

    delivery_mode = serializers.ChoiceField(choices=DeliveryMode.choices)
    scheduled_at = serializers.DateTimeField()

    def validate_scheduled_at(self, value: Any) -> Any:
        if value <= timezone.now():
            raise serializers.ValidationError("scheduled_at must be in the future.")
        return value


class PostSubmitRequestSerializer(serializers.Serializer[Any]):
    """`POST /posts/{id}/submit/` (P2-10).

    The schedule half is **optional and is a proposal**, not a schedule: it is
    parked on the post and consumed by the final approval, which calls the
    ordinary schedule service. Same shape as
    `PostScheduleRequestSerializer` above deliberately — the author fills in
    one form whether or not a reviewer stands between them and the calendar.
    """

    note = serializers.CharField(required=False, allow_blank=True, default="")
    delivery_mode = serializers.ChoiceField(
        choices=DeliveryMode.choices, required=False, allow_blank=True, default=""
    )
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True, default=None)

    def validate_scheduled_at(self, value: Any) -> Any:
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("scheduled_at must be in the future.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Both halves of the proposal or neither.

        A delivery mode with no time is unschedulable and a time with no mode
        is ambiguous between a reminder and a publish — either would be stored,
        silently ignored at approval, and discovered as "why didn't it go out".
        """
        if bool(attrs.get("delivery_mode")) != (attrs.get("scheduled_at") is not None):
            raise serializers.ValidationError(
                {"scheduled_at": "Propose both `delivery_mode` and `scheduled_at`, or neither."}
            )
        return attrs


class AdaptedMediaSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    kind = serializers.CharField()
    url = serializers.CharField()
    alt = serializers.CharField(allow_blank=True)


class AdaptedPayloadSerializer(serializers.Serializer[Any]):
    platform = serializers.CharField()
    #: The shape this was rendered as (P4-04). Served rather than left for the
    #: reader to infer: the caps that produced this media list came from the
    #: `(platform, format)` row, so a client re-deriving it could disagree
    #: with what was actually applied.
    post_format = serializers.CharField()
    body = serializers.CharField()
    thread = serializers.ListField(child=serializers.CharField())
    hashtags = serializers.ListField(child=serializers.CharField())
    media = AdaptedMediaSerializer(many=True)
    options = serializers.DictField()
    truncated = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())


class PostPreviewResponseSerializer(serializers.Serializer[Any]):
    payloads = serializers.DictField(child=AdaptedPayloadSerializer())


class PostTemplateSerializer(serializers.ModelSerializer[PostTemplate]):
    """`payload` is validated here against the same `rules.py` declaration a
    real post's options go through — on the way in, not at apply time
    (P1-09)."""

    created_by_email = serializers.EmailField(
        source="created_by.email", read_only=True, default=None
    )

    class Meta:
        model = PostTemplate
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "content_kind",
            "payload",
            "created_by_email",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "created_by_email",
            "created_at",
            "updated_at",
        )

    def validate_content_kind(self, value: str) -> str:
        templates.ensure_creatable(value)
        return value

    def validate_name(self, value: str) -> str:
        """Checked here as well as in the database. `workspace` is not a
        serializer field — it comes from the request — so DRF cannot build the
        unique-together validator itself, and the constraint alone would
        surface as a 500 rather than a field error the form can render.

        Resolved through the one resolver rather than re-derived: a second
        copy of "which workspace is this" is exactly the scattered tenancy
        logic `common/workspaces.py` exists to prevent."""
        request = self.context.get("request")
        workspace = request_workspace(request) if request is not None else None

        clashes = PostTemplate.objects.filter(workspace=workspace, name=value)
        if self.instance is not None:
            clashes = clashes.exclude(pk=self.instance.pk)
        if clashes.exists():
            raise serializers.ValidationError("A template with this name already exists.")
        return value

    def validate_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        return templates.validate_payload(value or {})


class RecurrenceRuleSerializer(serializers.ModelSerializer[RecurrenceRule]):
    """`source` is scoped to the caller's workspace, so a rule cannot be
    pointed at another tenant's template — a foreign id is simply not a choice.

    Scoped in `__init__` rather than redeclared as a class attribute:
    `source` is also the name of a `rest_framework.fields.Field` attribute, and
    shadowing it on a serializer class is the kind of collision that works
    until the day it does not.
    """

    class Meta:
        model = RecurrenceRule
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "source",
            "rrule",
            "timezone",
            "horizon_days",
            "active",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "created_at", "updated_at")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_workspace(
            self.fields["source"], self.context.get("request"), PostTemplate
        )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Whole-object rather than per-field: the frequency floor and the
        timezone have to be checked together, and an rrule is only valid
        *for* a zone."""
        instance = self.instance
        recurrence.validate(
            rrule=attrs.get("rrule", getattr(instance, "rrule", "")),
            timezone_name=attrs.get("timezone", getattr(instance, "timezone", "UTC")),
        )
        return attrs


class CropRequestSerializer(serializers.Serializer[Any]):
    """Pixels, top-left origin. Named fields rather than a 4-tuple because
    `[0, 0, 300, 300]` is ambiguous between (left, top, width, height) and
    (left, top, right, bottom), and the two differ silently."""

    left = serializers.IntegerField(min_value=0)
    top = serializers.IntegerField(min_value=0)
    width = serializers.IntegerField(min_value=1)
    height = serializers.IntegerField(min_value=1)


class TrimRequestSerializer(serializers.Serializer[Any]):
    start_ms = serializers.IntegerField(min_value=0)
    end_ms = serializers.IntegerField(min_value=1)


class PlatformOptionSerializer(serializers.Serializer[Any]):
    """One declared composer field, as `rules.py` states it.

    `label`, `required` and `source` shadow attributes of
    `rest_framework.fields.Field`. Harmless at runtime — DRF's metaclass moves
    declared fields off the class into `_declared_fields` before an instance
    exists — but mypy sees the class body, so the assignments are annotated.
    Renaming them is not an option: these are the keys the composer reads, and
    a wire format bent around a type checker is a wire format nobody can guess.

    **`source` deserves a specific warning.** On `Field` it is the binding
    that says *which attribute to read*, so passing `source=` to any field in
    this serializer would be changing where a value comes from, not naming
    this key. As a declared field name it is fine — it reads `instance
    ["source"]`, which is what the view puts there — and
    `test_a_remote_choice_option_is_served_with_its_source` is what keeps that
    true.
    """

    key = serializers.CharField()
    kind = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    choices = serializers.ListField(child=serializers.CharField())
    #: `remote_choice` only — which provider-backed list the composer should
    #: fetch (P4-02). Empty for every other kind, so the field is always
    #: present and the client never has to branch on its absence.
    source = serializers.CharField(allow_blank=True)  # type: ignore[assignment]
    max_length = serializers.IntegerField(allow_null=True)
    default = serializers.JSONField(allow_null=True)
    required = serializers.BooleanField()  # type: ignore[assignment]


class PlatformFormatSerializer(serializers.Serializer[Any]):
    """One `(platform, format)` row's constraints (P4-05).

    `char_limit` is served **resolved** — the format's own, or the platform's
    where the format does not narrow it. The composer needs the number that
    applies, not a null it would have to know the fallback rule to interpret.
    """

    format = serializers.CharField()
    max_media = serializers.IntegerField()
    min_media = serializers.IntegerField()
    allowed_media_kinds = serializers.ListField(child=serializers.CharField())
    char_limit = serializers.IntegerField()


class PlatformRuleSerializer(serializers.Serializer[Any]):
    platform = serializers.CharField()
    char_limit = serializers.IntegerField()
    supports_thread = serializers.BooleanField()
    formats = PlatformFormatSerializer(many=True)
    options = PlatformOptionSerializer(many=True)


class PlatformRuleListSerializer(serializers.Serializer[Any]):
    platforms = PlatformRuleSerializer(many=True)
