from __future__ import annotations

from typing import Any

from rest_framework import serializers

from checks.models import CheckRunState, CheckStatus, CheckVerdict


class CheckResultSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    status = serializers.ChoiceField(choices=CheckStatus.choices)
    subject = serializers.CharField(allow_blank=True)
    message = serializers.CharField()
    media = serializers.IntegerField(required=False)
    platform = serializers.CharField(required=False)
    platforms = serializers.ListField(child=serializers.CharField(), required=False)
    clause = serializers.CharField(required=False)
    source = serializers.CharField(required=False)  # type: ignore[assignment]
    excerpt = serializers.CharField(required=False)
    rewrite = serializers.CharField(required=False)
    provider = serializers.CharField(required=False)


class CheckCountsSerializer(serializers.Serializer[Any]):
    PASS = serializers.IntegerField()
    FIX = serializers.IntegerField()
    BLOCK = serializers.IntegerField()
    UNAVAILABLE = serializers.IntegerField()


class CheckSummarySerializer(serializers.Serializer[Any]):
    """A queue badge: computed from the same request as the list."""

    #: `NONE` when the post has never been checked.
    verdict = serializers.ChoiceField(choices=[*CheckVerdict.values, "NONE"])
    counts = CheckCountsSerializer()
    #: The post changed after this run; the gate re-checks before deciding.
    stale = serializers.BooleanField()
    run_at = serializers.DateTimeField(allow_null=True)


class CheckRunSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    state = serializers.ChoiceField(choices=CheckRunState.choices)
    verdict = serializers.CharField(allow_blank=True)
    #: `null` while the run is still going.
    counts = CheckCountsSerializer(allow_null=True)
    results = CheckResultSerializer(many=True)
    revision = serializers.IntegerField(allow_null=True)
    stale = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
