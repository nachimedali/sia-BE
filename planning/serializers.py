"""Planning serialisation.

Shapes only. Tenancy is the queryset's job, and every foreign key a client can
write is scoped to the caller's own workspace before a value reaches a service
— the one shared defence against referencing another tenant's row by id.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from common.workspaces import scope_related_field_to_workspace
from content.models import Post
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
from planning.services.campaigns import validate_goals


class CampaignSerializer(serializers.ModelSerializer[Campaign]):
    item_count = serializers.IntegerField(source="items.count", read_only=True)

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
            "created_at",
            "updated_at",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        scope_related_field_to_workspace(self.fields["brief"], self.context.get("request"), Post)

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
