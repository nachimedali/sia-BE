"""Digest and finding serialisation.

**Every finding is serialised with its grade and its sample size** (P7-G1).
Neither is optional and neither is computed here — both are columns, so a
surface cannot render a finding without also having what qualifies it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from learn.models import Digest, Finding
from taste.models import REASON_CODES, Decision, Rule


class FindingSerializer(serializers.ModelSerializer[Finding]):
    #: Segment mean over baseline mean, to two places — computed here so the
    #: page plots and prints the same figure and never derives its own.
    ratio = serializers.SerializerMethodField()

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
            "ratio",
        ]
        read_only_fields = fields

    def get_ratio(self, obj: Finding) -> float | None:
        segment = obj.comparison.get("segment_mean")
        baseline = obj.comparison.get("baseline_mean")
        if obj.excluded_reason or not isinstance(segment, int | float):
            return None
        if not isinstance(baseline, int | float) or baseline <= 0:
            return None
        return round(float(segment) / float(baseline), 2)


class RuleDecisionSerializer(serializers.ModelSerializer[Decision]):
    """Who said what about a proposal, and when."""

    class Meta:
        model = Decision
        fields: ClassVar[list[str]] = ["verdict", "reason_code", "note", "created_at"]
        read_only_fields = fields


class ProposedRuleSerializer(serializers.ModelSerializer[Rule]):
    """A rule Learn proposed, with the finding it came from (P7-11).

    `provenance` is the id of the `Finding`, so a reader can walk backwards
    from the proposal to the evidence without a second request shape.
    """

    ruleset_version = serializers.IntegerField(source="ruleset.version", read_only=True)
    is_accepted = serializers.SerializerMethodField()
    decision = serializers.SerializerMethodField()

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
            "decision",
        ]
        read_only_fields = fields

    def get_is_accepted(self, obj: Rule) -> bool:
        return obj.accepted_at is not None

    @extend_schema_field(RuleDecisionSerializer(allow_null=True))
    def get_decision(self, obj: Rule) -> dict[str, Any] | None:
        """The latest verdict on this proposal, so a rejection still reads as
        one after a reload. Decisions are append-only; the newest one stands."""
        # `.all()` so a digest read uses the viewset's prefetch.
        latest = max(obj.decisions.all(), key=lambda d: (d.created_at, d.pk), default=None)
        if latest is None:
            return None
        return dict(RuleDecisionSerializer(latest).data)


class ProseCheckSerializer(serializers.Serializer[dict[str, Any]]):
    """The stored narration re-checked against the stored statistics — the
    same two tests narration had to pass, run again on the document as kept."""

    numbers = serializers.IntegerField()
    grounded = serializers.BooleanField()
    predictive = serializers.BooleanField()


class DigestSerializer(serializers.ModelSerializer[Digest]):
    findings = FindingSerializer(many=True, read_only=True)
    proposed_rules = serializers.SerializerMethodField()
    campaign_name = serializers.CharField(source="campaign.name", default="", read_only=True)
    posts_measured = serializers.SerializerMethodField()
    prose_check = serializers.SerializerMethodField()

    class Meta:
        model = Digest
        fields: ClassVar[list[str]] = [
            "id",
            "campaign",
            "campaign_name",
            "posts_measured",
            "prose_check",
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

    def get_posts_measured(self, obj: Digest) -> int:
        """How many measured posts the window held: every finding compares a
        segment with the rest of the same window, so segment + baseline is the
        whole window, and the largest such sum is the count."""
        return max(
            (finding.sample_size + finding.baseline_size for finding in obj.findings.all()),
            default=0,
        )

    @extend_schema_field(ProseCheckSerializer)
    def get_prose_check(self, obj: Digest) -> dict[str, Any]:
        from learn.services.narrate import check_prose

        return check_prose(obj.narration or "", obj.statistics or {})

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
