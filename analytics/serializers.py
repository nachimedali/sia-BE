"""Analytics serialisation (design.md §7).

Mostly plain `Serializer`s over dataclasses rather than `ModelSerializer`s: what
the endpoints return is `analytics.services.signals`' derived view of the data,
not the capture rows themselves. A client that wanted raw captures would be
reading the pipeline's working state.

Every `SerializerMethodField` here is `@extend_schema_field`-annotated for the
same reason as elsewhere — an un-annotated one types as `unknown` in the
generated client.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from analytics.models import (
    AudienceComment,
    AudienceDemographic,
    Report,
    ReportRun,
    ReportShareLink,
    RepurposeCandidate,
)
from trends.models import TrendSource


class TargetPerformanceSerializer(serializers.Serializer[Any]):
    target_id = serializers.IntegerField()
    post_id = serializers.IntegerField()
    platform = serializers.CharField()
    published_at = serializers.DateTimeField()
    engagement_rate = serializers.FloatField()
    #: Null where the platform cannot report it — never 0 (rule 4).
    impressions = serializers.IntegerField(allow_null=True)
    likes = serializers.IntegerField(allow_null=True)
    comments = serializers.IntegerField(allow_null=True)
    #: Null until the account has enough history to rank against — the UI shows
    #: "not enough history yet" rather than a number that looks authoritative.
    percentile = serializers.FloatField(allow_null=True)
    decay_ratio = serializers.FloatField()


class AttributionBucketSerializer(serializers.Serializer[Any]):
    posts = serializers.IntegerField()
    engagement_rate = serializers.FloatField()


class BestTimeSerializer(serializers.Serializer[Any]):
    weekday = serializers.IntegerField(help_text="0 = Monday, 6 = Sunday.")
    hour = serializers.IntegerField()
    samples = serializers.IntegerField()
    engagement_rate = serializers.FloatField()


class OverviewSerializer(serializers.Serializer[Any]):
    posts = serializers.IntegerField()
    #: Null when no post's impressions could be measured.
    impressions = serializers.IntegerField(allow_null=True)
    #: Posts left out of `impressions` because their platform cannot report it.
    impressions_excluded = serializers.IntegerField()
    engagement_rate = serializers.FloatField()
    top = TargetPerformanceSerializer(many=True)
    #: Keyed by `PostSource` (`AI`/`MANUAL`) — §8.9's honest self-audit.
    by_source = serializers.DictField(child=AttributionBucketSerializer())
    #: Keyed by media kind (`IMAGE`/`VIDEO`/`TEXT`).
    by_media = serializers.DictField(child=AttributionBucketSerializer())
    #: Included here rather than left to the standalone endpoint: best times are
    #: a pure function of the same scan, so fetching them separately meant the
    #: page ran that scan twice.
    best_times = BestTimeSerializer(many=True)


class SentimentSummarySerializer(serializers.Serializer[Any]):
    total = serializers.IntegerField()
    positive = serializers.IntegerField()
    neutral = serializers.IntegerField()
    negative = serializers.IntegerField()
    #: Mean of the per-comment scores, -1 to 1.
    score = serializers.FloatField()


class CommentStateSerializer(serializers.Serializer[Any]):
    """Where a comment stands after what was done to it from the app."""

    hidden = serializers.BooleanField()
    liked = serializers.BooleanField()
    deleted = serializers.BooleanField()
    replies = serializers.IntegerField()


class CommentCapabilitiesSerializer(serializers.Serializer[Any]):
    """The platform's own flags for one comment (SA-62)."""

    can_reply = serializers.BooleanField()
    can_hide = serializers.BooleanField()
    can_like = serializers.BooleanField()
    can_delete = serializers.BooleanField()
    can_private_reply = serializers.BooleanField()


#: The platform's five flags, in the order the serializer declares them.
CAPABILITY_FLAGS = ("can_reply", "can_hide", "can_like", "can_delete", "can_private_reply")


def normalised_capabilities(raw: dict[str, Any] | None) -> dict[str, bool] | None:
    """The five flags, every one present. `None` stays `None` (the provider said
    nothing: offer no action on its behalf); a provider that sent only some of
    them has said "no" to the rest, which is what `_allowed` already does — a
    partial dict must not 500 the feed."""
    if raw is None:
        return None
    return {key: bool(raw.get(key)) for key in CAPABILITY_FLAGS}


class CommentSerializer(serializers.ModelSerializer[AudienceComment]):
    capabilities = serializers.SerializerMethodField()
    #: `false` where the platform gives no reaction breakdown (SA-61) — shown
    #: as "not shared", never as zero.
    reactions_shared = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()

    class Meta:
        model = AudienceComment
        fields = (
            "id",
            "post_target",
            "author",
            "body",
            "sentiment",
            "sentiment_score",
            "posted_at",
            "capabilities",
            "reactions_shared",
            "state",
        )
        read_only_fields = fields

    def get_reactions_shared(self, comment: AudienceComment) -> bool:
        return comment.reactions is not None

    @extend_schema_field(CommentCapabilitiesSerializer(allow_null=True))
    def get_capabilities(self, comment: AudienceComment) -> dict[str, bool] | None:
        return normalised_capabilities(comment.capabilities)

    @extend_schema_field(CommentStateSerializer)
    def get_state(self, comment: AudienceComment) -> dict[str, Any]:
        from analytics.services.audience import state_of

        return dict(CommentStateSerializer(state_of(comment)).data)


class CommentActionSerializer(serializers.Serializer[dict[str, str]]):
    action = serializers.ChoiceField(choices=["hide", "unhide", "like", "unlike", "delete"])


class RepurposeCandidateSerializer(serializers.ModelSerializer[RepurposeCandidate]):
    post_body = serializers.CharField(source="post.master_body", read_only=True)

    class Meta:
        model = RepurposeCandidate
        fields = (
            "id",
            "post",
            "post_body",
            "published_at",
            "percentile",
            "reason",
            "score",
            "surfaced_at",
        )
        read_only_fields = fields


class AudienceReplySerializer(serializers.Serializer[dict[str, str]]):
    """The body of an outbound reply (P0-36).

    Length-capped at the tightest platform limit rather than the loosest: a
    reply accepted here and rejected by the platform costs the org's allowance
    for nothing, because the debit is already recorded by then.
    """

    body = serializers.CharField(max_length=1000, trim_whitespace=True)


class AudienceReplyResultSerializer(serializers.Serializer[dict[str, str]]):
    external_id = serializers.CharField()


# -----------------------------------------------------------------------------
# Reporting (Phase 6)
# -----------------------------------------------------------------------------
class ReportSerializer(serializers.ModelSerializer[Report]):
    """A report definition. `sections` is validated against the registry in
    `services.reporting`, not here — one declaration of what a section can be,
    read by the API and by the monthly job alike."""

    class Meta:
        model = Report
        fields = ("id", "name", "sections", "schedule", "window_days", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_sections(self, value: Any) -> Any:
        from analytics.services.reporting import validate_sections

        return validate_sections(value)


class ReportRunSerializer(serializers.ModelSerializer[ReportRun]):
    document_url = serializers.SerializerMethodField()

    class Meta:
        model = ReportRun
        fields = (
            "id",
            "report",
            "window_start",
            "window_end",
            "status",
            "payload",
            "document_url",
            "error_detail",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_blank=True))
    def get_document_url(self, obj: ReportRun) -> str:
        return obj.document.url if obj.document else ""


class ReportRunRequestSerializer(serializers.Serializer[dict[str, Any]]):
    """An explicit window, or none at all.

    Omitting both means "the report's own `window_days`, ending now", which is
    what the button on the reports page sends. A half-specified range is
    refused rather than guessed — a report covering a window nobody chose is
    the thing P6-09 exists to prevent, one step earlier.
    """

    starts_at = serializers.DateTimeField(required=False)
    ends_at = serializers.DateTimeField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if ("starts_at" in attrs) != ("ends_at" in attrs):
            raise serializers.ValidationError("Give both 'starts_at' and 'ends_at', or neither.")
        if attrs.get("starts_at") and attrs["starts_at"] >= attrs["ends_at"]:
            raise serializers.ValidationError("'starts_at' must be before 'ends_at'.")
        return attrs


class ReportShareSerializer(serializers.Serializer[dict[str, Any]]):
    email = serializers.EmailField(required=False, allow_blank=True, default="")


class ReportShareLinkSerializer(serializers.ModelSerializer[ReportShareLink]):
    """**The raw token is never in here.** It exists once, in the response to
    the call that minted it, and is returned by the view rather than by this
    serializer so a later list endpoint cannot start leaking it."""

    class Meta:
        model = ReportShareLink
        fields = ("id", "run", "email", "expires_at", "revoked_at", "created_at")
        read_only_fields = fields


class SharedReportSerializer(serializers.Serializer[dict[str, Any]]):
    """What a guest with a link sees. Frozen payload, no tenant identifiers
    beyond the workspace's own name."""

    report_name = serializers.CharField()
    workspace_name = serializers.CharField()
    window_start = serializers.DateTimeField()
    window_end = serializers.DateTimeField()
    payload = serializers.JSONField()
    document_url = serializers.CharField(allow_blank=True)
    expires_at = serializers.DateTimeField()
    shared_at = serializers.DateTimeField()
    brand_url = serializers.CharField(allow_blank=True)


class AudienceDemographicSerializer(serializers.ModelSerializer[AudienceDemographic]):
    """`breakdown` is null on an `UNAVAILABLE` row, and the surface must render
    that as "we cannot see this yet" rather than as an empty chart."""

    platform = serializers.CharField(source="social_account.platform", read_only=True)
    handle = serializers.CharField(source="social_account.handle", read_only=True)

    class Meta:
        model = AudienceDemographic
        fields = (
            "id",
            "platform",
            "handle",
            "dimension",
            "breakdown",
            "availability",
            "captured_at",
        )
        read_only_fields = fields


class CompetitorSerializer(serializers.ModelSerializer[TrendSource]):
    class Meta:
        model = TrendSource
        fields = ("id", "platform", "handle", "label", "is_active", "created_at")
        read_only_fields = ("id", "is_active", "created_at")


class CompetitorComparisonSerializer(serializers.Serializer[dict[str, Any]]):
    """Both sides are interactions over audience — the same calculation, which
    is what makes the two numbers beside each other comparable at all.

    Every rate is nullable and means it: an account whose follower count the
    vendor did not report is unmeasured, not unengaging.
    """

    platform = serializers.CharField()
    window_days = serializers.IntegerField()
    us = serializers.JSONField()
    competitors = serializers.JSONField()


# --- the analytics page in one read (`analytics.services.summary`, SA-70) -------
class SummaryCellSerializer(serializers.Serializer[Any]):
    """A figure, `pending` or `unavailable` — with the reason when not a number.
    A measured 0 is `state="measured", value=0`; nothing else is ever 0."""

    state = serializers.ChoiceField(choices=["measured", "pending", "unavailable"])
    value = serializers.FloatField(allow_null=True)
    why = serializers.CharField(allow_blank=True)


class SummaryChangeSerializer(serializers.Serializer[Any]):
    previous = serializers.IntegerField()
    current = serializers.IntegerField()
    #: Null when the previous period measured nothing to compare with.
    percent = serializers.IntegerField(allow_null=True)
    accounts_left_out = serializers.IntegerField()


class SummaryExcludedSerializer(serializers.Serializer[Any]):
    account = serializers.CharField()
    why = serializers.CharField()


class SummaryTotalSerializer(serializers.Serializer[Any]):
    total = serializers.IntegerField(allow_null=True)
    accounts_counted = serializers.IntegerField()
    excluded = SummaryExcludedSerializer(many=True)
    partial_days = serializers.IntegerField()
    pending_days = serializers.IntegerField()
    change = SummaryChangeSerializer(allow_null=True)


class SummaryDaySerializer(serializers.Serializer[Any]):
    date = serializers.DateField()
    total = serializers.IntegerField(allow_null=True)
    by_platform = serializers.DictField(child=serializers.IntegerField())
    #: Accounts whose captures for that day's posts are incomplete or stale.
    partial_reasons = serializers.ListField(child=serializers.CharField())
    #: Platforms still inside their reporting delay for that day's posts.
    pending_platforms = serializers.ListField(child=serializers.CharField())
    posts = serializers.IntegerField()


class SummarySnapshotSerializer(serializers.Serializer[Any]):
    rung = serializers.CharField()
    at = serializers.DateTimeField()
    state = serializers.ChoiceField(choices=["captured", "missed", "due"])


class SummaryPostSerializer(serializers.Serializer[Any]):
    target_id = serializers.IntegerField()
    post_id = serializers.IntegerField()
    title = serializers.CharField(allow_blank=True)
    platform = serializers.CharField()
    account = serializers.CharField()
    format = serializers.CharField(allow_blank=True)
    published_at = serializers.DateTimeField()
    cells = serializers.DictField(child=SummaryCellSerializer())
    engagement_rate = SummaryCellSerializer()
    capture = serializers.ChoiceField(
        choices=["complete", "capturing", "pending", "partial", "stale"]
    )
    capture_why = serializers.CharField()
    snapshots = SummarySnapshotSerializer(many=True)


class SummaryAccountSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    platform = serializers.CharField()
    handle = serializers.CharField(allow_blank=True)
    display_name = serializers.CharField(allow_blank=True)
    #: The connections page's word for it (`channels.overview.STATE_BY_STATUS`).
    state = serializers.CharField()
    insights_off = serializers.BooleanField()
    followers = serializers.IntegerField(allow_null=True)
    followers_change_7d = serializers.IntegerField(allow_null=True)
    last_synced_at = serializers.DateTimeField(allow_null=True)
    history_days_held = serializers.IntegerField()


class SummaryWindowSerializer(serializers.Serializer[Any]):
    days = serializers.IntegerField()
    locked = serializers.BooleanField()


class AnalyticsSummarySerializer(serializers.Serializer[Any]):
    window = serializers.IntegerField()
    #: What the page opens on for this plan; a link omits `window` when equal.
    default_window = serializers.IntegerField()
    windows = SummaryWindowSerializer(many=True)
    history_days = serializers.IntegerField()
    start = serializers.DateField()
    end = serializers.DateField()
    comparable = serializers.BooleanField()
    totals = serializers.DictField(child=SummaryTotalSerializer())
    daily = serializers.DictField(child=SummaryDaySerializer(many=True))
    accounts = SummaryAccountSerializer(many=True)
    posts = SummaryPostSerializer(many=True)
    posts_truncated = serializers.BooleanField()
