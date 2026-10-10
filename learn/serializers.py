"""Digest and finding serialisation.

**Every finding is serialised with its grade and its sample size** (P7-G1).
Neither is optional and neither is computed here — both are columns, so a
surface cannot render a finding without also having what qualifies it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from learn.models import Digest, Finding
from taste.models import REASON_CODES, Rule


class FindingSerializer(serializers.ModelSerializer[Finding]):
    class Meta:
        model = Finding
        fields: ClassVar[list[str]] = [
            "id",
            "segment",
            "comparison",
            "confidence",
            "sample_size",
            "baseline_size",
            "campaigns_observed",
            "excluded_reason",
        ]
        read_only_fields = fields


class ProposedRuleSerializer(serializers.ModelSerializer[Rule]):
    """A rule Learn proposed, with the finding it came from (P7-11).

    `provenance` is the id of the `Finding`, so a reader can walk backwards
    from the proposal to the evidence without a second request shape.
    """

    ruleset_version = serializers.IntegerField(source="ruleset.version", read_only=True)
    is_accepted = serializers.SerializerMethodField()

    class Meta:
        model = Rule
        fields: ClassVar[list[str]] = [
            "id",
            "kind",
            "payload",
            "provenance",
            "ruleset_version",
            "is_accepted",
            "accepted_at",
        ]
        read_only_fields = fields

    def get_is_accepted(self, obj: Rule) -> bool:
        return obj.accepted_at is not None


class DigestSerializer(serializers.ModelSerializer[Digest]):
    findings = FindingSerializer(many=True, read_only=True)
    proposed_rules = serializers.SerializerMethodField()

    class Meta:
        model = Digest
        fields: ClassVar[list[str]] = [
            "id",
            "campaign",
            "generated_at",
            "window_start",
            "window_end",
            "narration",
            "narration_source",
            "statistics",
            "taste_profile_version",
            "ruleset_version",
            "findings",
            "proposed_rules",
        ]
        read_only_fields = fields

    def get_proposed_rules(self, obj: Digest) -> list[dict[str, Any]]:
        # Reads `obj.proposed_rulesets` (the `RuleSet.derived_from` reverse
        # relation) rather than a fresh `Rule` query, so this is free when the
        # viewset has prefetched `"proposed_rulesets__rules"`.
        rules = [rule for ruleset in obj.proposed_rulesets.all() for rule in ruleset.rules.all()]
        return list(ProposedRuleSerializer(rules, many=True).data)


class RunLearnSerializer(serializers.Serializer[dict[str, Any]]):
    """What an on-demand run accepts. A campaign, or the workspace at large."""

    campaign = serializers.IntegerField(required=False, allow_null=True)


class RuleVerdictSerializer(serializers.Serializer[dict[str, Any]]):
    """Accepting or rejecting one proposal (P7-11)."""

    #: The same fixed, admin-extensible vocabulary a candidate rejection uses
    #: (`taste.serializers.CandidateDecisionRequestSerializer`) — this reason
    #: lands in the same `Decision.reason_code` column.
    reason_code = serializers.ChoiceField(choices=REASON_CODES, required=False)
    note = serializers.CharField(required=False, allow_blank=True)
