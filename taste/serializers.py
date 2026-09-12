"""Taste, candidate and decision serialisation.

Shapes only. Every authority question is answered by the queryset before a
serializer sees a row — `screened_out` candidates in particular are filtered in
`services.candidates.review_queue`, so there is nothing here that could leak
one (P5-G1).
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from taste.models import (
    REASON_CODES,
    ContentCandidate,
    Decision,
    ProductTasteOverride,
    Rule,
    RuleSet,
    TasteProfile,
)
from taste.services.screening import validate_constraints


class TasteProfileSerializer(serializers.ModelSerializer[TasteProfile]):
    completeness = serializers.SerializerMethodField()

    class Meta:
        model = TasteProfile
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "version",
            "is_active",
            "hard_constraints",
            "voice",
            "structural",
            "topic_posture",
            "completeness",
            "created_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            # Both are service decisions: `version` is minted under a lock and
            # `is_active` is a partial unique index away from being impossible
            # to set twice. A client that could write either would be writing
            # the one fact attribution depends on.
            "version",
            "is_active",
            "completeness",
            "created_at",
        )

    def get_completeness(self, profile: TasteProfile) -> float:
        from taste.services.profiles import completeness

        return round(completeness(profile), 3)

    def validate_hard_constraints(self, value: Any) -> Any:
        return validate_constraints(value)


class ProductTasteOverrideSerializer(serializers.ModelSerializer[ProductTasteOverride]):
    class Meta:
        model = ProductTasteOverride
        fields: ClassVar[tuple[str, ...]] = ("id", "product", "constraints", "created_at")
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "created_at")

    def validate_constraints(self, value: Any) -> Any:
        return validate_constraints(value)


class RuleSerializer(serializers.ModelSerializer[Rule]):
    class Meta:
        model = Rule
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "kind",
            "payload",
            "accepted_by",
            "accepted_at",
            "created_at",
        )
        # Acceptance is its own endpoint: **Learn proposes, humans activate**
        # (Part 7 rule 14), and a PATCH that could set `accepted_at` would be
        # a rule activating itself.
        read_only_fields: ClassVar[tuple[str, ...]] = (
            "id",
            "accepted_by",
            "accepted_at",
            "created_at",
        )


class RuleSetSerializer(serializers.ModelSerializer[RuleSet]):
    rules = RuleSerializer(many=True, read_only=True)

    class Meta:
        model = RuleSet
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "version",
            "is_active",
            "rules",
            "created_at",
        )
        read_only_fields = fields


class DecisionSerializer(serializers.ModelSerializer[Decision]):
    """What was decided, and under which versions.

    `edit_diff` is **absent here**. It is the highest-value signal in the
    system and `admin`-only to read (P5-13), so it is served by its own
    endpoint rather than riding along on every queue render — a field that
    leaks by being on a list nobody thought about is the ordinary way this
    goes wrong.
    """

    class Meta:
        model = Decision
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "verdict",
            "reason_code",
            "note",
            "actor",
            "taste_profile_version",
            "ruleset_version",
            "model_identity",
            "prompt_template_version",
            "created_at",
        )
        read_only_fields = fields


class ContentCandidateSerializer(serializers.ModelSerializer[ContentCandidate]):
    decisions = DecisionSerializer(many=True, read_only=True)

    class Meta:
        model = ContentCandidate
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "state",
            "payload",
            "product",
            "campaign",
            "taste_profile",
            "parent",
            "parent_reason_code",
            "expires_at",
            "post",
            "decisions",
            "created_at",
        )
        # Every transition is a service call that writes a `Decision` — a
        # client-settable `state` would be the bypass C-01 exists to prevent.
        read_only_fields = fields


class CandidateDecisionRequestSerializer(serializers.Serializer[Any]):
    """Approve or reject, in one shape.

    `edited_payload` is what makes `accepted_with_edits` possible: the diff
    between what was shown and what was kept is the most valuable thing this
    system records (P5-13).
    """

    reason_code = serializers.ChoiceField(choices=REASON_CODES, required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    edited_payload = serializers.DictField(required=False)
