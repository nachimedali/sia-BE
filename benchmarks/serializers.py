"""Benchmark request and response shapes.

The response serializers exist for the schema as much as for the output: the
frontend's types are generated from them, so "a sub-threshold cohort has no
median" is a fact the TypeScript compiler knows (P8-G1) rather than a comment
the page has to remember. Every statistic is `required=False` and is omitted,
never nulled, when the reader did not produce it.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from benchmarks.models import ConsentAction


class GrantConsentSerializer(serializers.Serializer[dict[str, Any]]):
    #: The version the person was shown. Refused if it is no longer current.
    policy_version = serializers.IntegerField(min_value=1)
    #: ISO 3166-1 alpha-2. Validated by the service against the tz database.
    market = serializers.CharField(max_length=2)


class BenchmarkPolicySerializer(serializers.Serializer[dict[str, Any]]):
    version = serializers.IntegerField()
    summary = serializers.CharField()
    document_url = serializers.CharField(allow_blank=True)
    published_at = serializers.DateTimeField()


class BenchmarkConsentSerializer(serializers.Serializer[dict[str, Any]]):
    policy_version = serializers.IntegerField()
    granted_at = serializers.DateTimeField()


class BenchmarkVerticalSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class BenchmarkMarketSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    name = serializers.CharField()


class BenchmarkConsentEventSerializer(serializers.Serializer[dict[str, Any]]):
    action = serializers.ChoiceField(choices=ConsentAction.choices)
    policy_version = serializers.IntegerField()
    recorded_at = serializers.DateTimeField()


class BenchmarkParticipationSerializer(serializers.Serializer[dict[str, Any]]):
    contributing = serializers.BooleanField()
    requires_reconsent = serializers.BooleanField()
    policy = BenchmarkPolicySerializer(allow_null=True)
    consent = BenchmarkConsentSerializer(allow_null=True)
    market = serializers.CharField(allow_blank=True)
    vertical = BenchmarkVerticalSerializer(allow_null=True)
    markets = BenchmarkMarketSerializer(many=True)
    history = BenchmarkConsentEventSerializer(many=True)


class BenchmarkShortfallSerializer(serializers.Serializer[dict[str, Any]]):
    workspaces = serializers.IntegerField()
    posts = serializers.IntegerField()


_STATUSES = ("ok", "insufficient_cohort_data", "pending")


class BenchmarkMetricSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=_STATUSES[:2])
    median = serializers.FloatField(required=False)
    p25 = serializers.FloatField(required=False)
    p75 = serializers.FloatField(required=False)
    posts = serializers.IntegerField(required=False)
    shortfall = BenchmarkShortfallSerializer(required=False)


class BenchmarkWindowSerializer(serializers.Serializer[dict[str, Any]]):
    window = serializers.CharField()
    median_engagement_rate = serializers.FloatField()
    posts = serializers.IntegerField()


class BenchmarkCohortSerializer(serializers.Serializer[dict[str, Any]]):
    platform = serializers.CharField()
    size_band = serializers.CharField()
    post_format = serializers.CharField()
    status = serializers.ChoiceField(choices=_STATUSES)
    contributors = serializers.IntegerField(required=False)
    posts = serializers.IntegerField(required=False)
    shortfall = BenchmarkShortfallSerializer(required=False)
    metrics = serializers.DictField(child=BenchmarkMetricSerializer(), required=False)
    best_windows = BenchmarkWindowSerializer(many=True, required=False)


class BenchmarkRunSerializer(serializers.Serializer[dict[str, Any]]):
    computed_at = serializers.DateTimeField()
    window_start = serializers.DateField()
    window_end = serializers.DateField()


class BenchmarksSerializer(serializers.Serializer[dict[str, Any]]):
    run = BenchmarkRunSerializer(allow_null=True)
    cohorts = BenchmarkCohortSerializer(many=True)
