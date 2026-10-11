"""Planning serialisation.

Shapes only. Tenancy is the queryset's job, and every foreign key a client can
write is scoped to the caller's own workspace before a value reaches a service
— the one shared defence against referencing another tenant's row by id.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from billing.services.flags import CAMPAIGN_BRIEF_S5, flag_enabled
from common.workspaces import scope_related_field_to_workspace
from content.models import Platform, Post
from planning.models import (
    BulkOperation,
    BulkOperationItem,
    Campaign,
    CampaignItem,
    CampaignStatus,
    Label,
    SavedView,
    Timetable,
)
from planning.services import progress as progress_service
from planning.services.campaigns import GOAL_METRICS, validate_goals


class CampaignProgressSerializer(serializers.Serializer[Any]):
    """Progress against the first goal (steps-plan S5). `actual` is measured
    or `None` — never a zero standing in for "nothing reported"."""

    state = serializers.ChoiceField(
        choices=["no_goal", "not_started", "not_enough_data", "measured"]
    )
    metric = serializers.CharField(allow_null=True)
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]
    target = serializers.FloatField(allow_null=True)
    actual = serializers.FloatField(allow_null=True)
    measured_posts = serializers.IntegerField()
    measured_to = serializers.DateTimeField(allow_null=True)
    day = serializers.IntegerField(allow_null=True)
    days_total = serializers.IntegerField()


class ProgressDaySerializer(serializers.Serializer[Any]):
    date = serializers.DateField()
    value = serializers.FloatField(allow_null=True)


class CampaignProgressDetailSerializer(CampaignProgressSerializer):
    days = ProgressDaySerializer(many=True)
    gap_days = serializers.IntegerField()


class CampaignSerializer(serializers.ModelSerializer[Campaign]):
    item_count = serializers.IntegerField(source="items.count", read_only=True)
    progress = serializers.SerializerMethodField()
    has_plan = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "brief",
            "starts_at",
            "ends_at",
            "kind",
            "status",
            "goals",
            "planned_volume",
            "closed_at",
            "item_count",
            "progress",
            "has_plan",
            "digest",
            "created_by_name",
            "created_at",
            "updated_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            # Status moves through its own endpoint so an illegal transition is
            # a 409 from one place, rather than a PATCH that silently succeeds.
            "status",
            "closed_at",
            "item_count",
            "digest",
            "created_at",
            "updated_at",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_workspace(self.fields["brief"], self.context.get("request"), Post)

    def _s5(self, obj: Campaign) -> bool:
        return flag_enabled(obj.workspace.organization, CAMPAIGN_BRIEF_S5)

    @extend_schema_field(CampaignProgressSerializer(allow_null=True))
    def get_progress(self, obj: Campaign) -> dict[str, Any] | None:
        return progress_service.summary(obj) if self._s5(obj) else None

    def get_has_plan(self, obj: Campaign) -> bool:
        return obj.plans.filter(status="SUCCEEDED").exists()

    def get_created_by_name(self, obj: Campaign) -> str:
        user = obj.created_by
        if user is None:
            return ""
        return user.get_full_name() or user.email.split("@")[0]

    def validate_goals(self, value: Any) -> Any:
        return validate_goals(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "A campaign ends after it starts."})
        return attrs


class CampaignStatusRequestSerializer(serializers.Serializer[Any]):
    status = serializers.ChoiceField(choices=CampaignStatus.choices)


class CampaignItemSerializer(serializers.ModelSerializer[CampaignItem]):
    class Meta:
        model = CampaignItem
        fields: ClassVar[tuple[str, ...]] = ("id", "post", "added_by", "added_at")
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "added_by", "added_at")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_workspace(self.fields["post"], self.context.get("request"), Post)


class BriefGoalSerializer(serializers.Serializer[Any]):
    metric = serializers.ChoiceField(choices=sorted(GOAL_METRICS))
    target = serializers.FloatField(min_value=0)


class BriefConstraintsSerializer(serializers.Serializer[Any]):
    """What the team can do. Every number is the user's, quoted as theirs."""

    team = serializers.IntegerField(min_value=0, max_value=500, required=False, allow_null=True)
    hours_per_week = serializers.IntegerField(
        min_value=0, max_value=168, required=False, allow_null=True
    )
    budget = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    #: Optional here so the form can autosave as it is filled in; the brief's
    #: readiness (`briefs.missing`) is what requires both before a plan.
    posts_per_week = serializers.IntegerField(
        min_value=1, max_value=21, required=False, allow_null=True
    )
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=Platform.choices), max_length=12, required=False
    )


class ProofOptionSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    kind = serializers.ChoiceField(choices=["core", "measured", "you"])
    text = serializers.CharField()
    source = serializers.CharField()  # type: ignore[assignment]


class CampaignBriefWriteSerializer(serializers.Serializer[Any]):
    """A partial save: only the keys sent change."""

    goal = BriefGoalSerializer(required=False, allow_null=True)
    starts_at = serializers.DateTimeField(required=False)
    ends_at = serializers.DateTimeField(required=False)
    audience = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    segments = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, max_length=12
    )
    offer = serializers.CharField(required=False, allow_blank=True, max_length=300)
    proof_keys = serializers.ListField(
        child=serializers.CharField(max_length=120), required=False, max_length=20
    )
    own_proof = serializers.ListField(
        child=serializers.CharField(max_length=300), required=False, max_length=10
    )
    constraints = BriefConstraintsSerializer(required=False)


class CampaignBriefSerializer(serializers.Serializer[Any]):
    goal = BriefGoalSerializer(allow_null=True)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    audience = serializers.CharField()
    segments = serializers.ListField(child=serializers.CharField())
    offer = serializers.CharField()
    proof_keys = serializers.ListField(child=serializers.CharField())
    own_proof = serializers.ListField(child=serializers.CharField())
    constraints = serializers.DictField()
    proof_options = ProofOptionSerializer(many=True)
    connected_channels = serializers.ListField(child=serializers.CharField())
    voice = serializers.CharField()
    missing = serializers.ListField(child=serializers.CharField())
    ready = serializers.BooleanField()
    updated_at = serializers.DateTimeField(allow_null=True)


class PlanLineSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    credits = serializers.IntegerField()


class PlanEstimateSerializer(serializers.Serializer[Any]):
    estimate_id = serializers.CharField()
    credits = serializers.IntegerField()
    lines = PlanLineSerializer(many=True)
    available = serializers.IntegerField(allow_null=True)


class PlanStartRequestSerializer(serializers.Serializer[Any]):
    estimate_id = serializers.CharField(max_length=32)


class CampaignPlanSerializer(serializers.Serializer[Any]):
    """`plan` is `{messages, channels, metrics, calendar, voice}`; typed on the
    frontend from this description rather than a nested schema, because its
    shape is the plan service's to keep, versioned with the row."""

    id = serializers.IntegerField()
    version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"])
    phase = serializers.CharField()
    progress = serializers.FloatField()
    credits = serializers.IntegerField()
    lines = PlanLineSerializer(many=True)
    written_by = serializers.CharField()
    error_code = serializers.CharField()
    error = serializers.CharField()
    plan = serializers.DictField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)


class SeedResultSerializer(serializers.Serializer[Any]):
    created = serializers.IntegerField()
    skipped = serializers.IntegerField()


class CampaignCountsSerializer(serializers.Serializer[Any]):
    ALL = serializers.IntegerField()
    PLANNING = serializers.IntegerField()
    ACTIVE = serializers.IntegerField()
    CLOSED = serializers.IntegerField()


class LabelSerializer(serializers.ModelSerializer[Label]):
    class Meta:
        model = Label
        fields: ClassVar[tuple[str, ...]] = ("id", "name", "colour", "created_at")
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "created_at")


class SavedViewSerializer(serializers.ModelSerializer[SavedView]):
    class Meta:
        model = SavedView
        fields: ClassVar[tuple[str, ...]] = ("id", "name", "filters", "created_at")
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "created_at")

    def validate_filters(self, value: Any) -> Any:
        from planning.services.views import validate_filters

        return validate_filters(value)


class TimetableSerializer(serializers.ModelSerializer[Timetable]):
    class Meta:
        model = Timetable
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "timezone",
            "slots",
            "is_default",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "created_at")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        from planning.services.timetables import validate_timetable

        # Validated together, not per field: a slot is only meaningful against
        # the zone it is read in, and a serializer that checked them apart
        # would accept "25:00" in a valid zone.
        validate_timetable(
            attrs.get("slots", getattr(self.instance, "slots", [])),
            timezone_name=attrs.get("timezone", getattr(self.instance, "timezone", "UTC")),
        )
        return attrs


class BulkOperationItemSerializer(serializers.ModelSerializer[BulkOperationItem]):
    class Meta:
        model = BulkOperationItem
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "post",
            "post_ref",
            "status",
            "error",
            "settled_at",
        )
        read_only_fields = fields


class BulkOperationSerializer(serializers.ModelSerializer[BulkOperation]):
    """Carries its items inline.

    The per-item outcome **is** the report (P3-G1), so a caller that fetched
    the operation and then had to fetch the failures separately would be one
    forgotten request away from drawing a green tick over a half-done job.
    """

    items = BulkOperationItemSerializer(many=True, read_only=True)

    class Meta:
        model = BulkOperation
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "action",
            "payload",
            "status",
            "total_count",
            "succeeded_count",
            "failed_count",
            "items",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class BulkOperationRequestSerializer(serializers.Serializer[Any]):
    """Either `post_ids` or `filters` — never both (P3-09).

    `filters` is what makes "select all 1,200 matching" honest: the client
    never enumerates the ids, so the 100-row page cap cannot silently shrink
    the selection to the first page.
    """

    action = serializers.CharField()
    post_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=False
    )
    filters = serializers.DictField(required=False)
    payload = serializers.DictField(required=False, default=dict)
