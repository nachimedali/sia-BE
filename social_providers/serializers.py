"""The cockpit's response shape (SA-80) — typed, so the page's types are
generated from it rather than written by hand."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class _Serializer(serializers.Serializer[Any]):
    alerts = serializers.ListField(child=serializers.CharField())


class LatencySerializer(serializers.Serializer[Any]):
    p50 = serializers.IntegerField(allow_null=True)
    p95 = serializers.IntegerField(allow_null=True)


class ProviderErrorSerializer(serializers.Serializer[Any]):
    kind = serializers.CharField()
    code = serializers.CharField(allow_blank=True)
    count = serializers.IntegerField()
    last_request_id = serializers.CharField(allow_blank=True)


class RateSeenSerializer(serializers.Serializer[Any]):
    rate_limit = serializers.IntegerField(allow_null=True)
    rate_remaining = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField()


class PortSerializer(serializers.Serializer[Any]):
    built = serializers.BooleanField()
    #: The provider key serving it, or `fake`.
    serving = serializers.CharField()


class ProviderStatusSerializer(serializers.Serializer[Any]):
    provider = serializers.CharField()
    name = serializers.CharField(source="label")
    has_credential = serializers.BooleanField()
    fake_mode = serializers.CharField()
    ports = serializers.DictField(child=PortSerializer())


class UsageSerializer(serializers.Serializer[Any]):
    """The vendor's `GET /usage`. A limit of -1 is unlimited."""

    period_start = serializers.CharField(required=False, allow_null=True)
    period_end = serializers.CharField(required=False, allow_null=True)
    brands_used = serializers.IntegerField(required=False, allow_null=True)
    brands_limit = serializers.IntegerField(required=False, allow_null=True)
    posts_used = serializers.IntegerField(required=False, allow_null=True)
    posts_limit = serializers.IntegerField(required=False, allow_null=True)
    interactions_used = serializers.IntegerField(required=False, allow_null=True)
    interactions_limit = serializers.IntegerField(required=False, allow_null=True)


class EndpointSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    url = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)
    events = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class ProviderPanelSerializer(_Serializer):
    status = ProviderStatusSerializer()
    #: Null when the vendor could not be read (`usage_error` says why).
    usage = UsageSerializer(allow_null=True)
    usage_error = serializers.CharField(allow_blank=True)
    rate_limit_seen = RateSeenSerializer(allow_null=True)
    calls_last_hour = serializers.DictField(child=serializers.IntegerField())
    latency_ms = LatencySerializer()
    errors_24h = ProviderErrorSerializer(many=True)


class CockpitAccountSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    workspace = serializers.CharField()
    platform = serializers.CharField()
    handle = serializers.CharField(allow_blank=True)
    status = serializers.CharField()
    insights_off = serializers.BooleanField()
    reconnect_reason = serializers.CharField(allow_blank=True)
    last_read_at = serializers.DateTimeField(allow_null=True)


class AccountsPanelSerializer(_Serializer):
    accounts = CockpitAccountSerializer(many=True)


class StuckPublishSerializer(serializers.Serializer[Any]):
    target_id = serializers.IntegerField()
    workspace = serializers.CharField()
    platform = serializers.CharField()
    provider_post_id = serializers.CharField()
    since = serializers.DateTimeField(allow_null=True)


class PublishingPanelSerializer(_Serializer):
    scheduled_next_24h = serializers.IntegerField()
    in_flight = serializers.IntegerField()
    stuck = StuckPublishSerializer(many=True)
    published_7d = serializers.IntegerField()
    failed_7d = serializers.IntegerField()
    failures_by_code_7d = serializers.DictField(child=serializers.IntegerField())
    drafts_open = serializers.IntegerField()


class WebhooksPanelSerializer(_Serializer):
    secret_configured = serializers.BooleanField()
    #: The endpoints registered at the vendor; null when unreadable.
    endpoints = EndpointSerializer(many=True, allow_null=True)
    endpoints_error = serializers.CharField(allow_blank=True)
    last_received_at = serializers.DateTimeField(allow_null=True)
    by_event_24h = serializers.DictField(child=serializers.IntegerField())
    by_status_24h = serializers.DictField(child=serializers.IntegerField())
    refused_today = serializers.DictField(child=serializers.IntegerField())


class CoverageCellSerializer(serializers.Serializer[Any]):
    rows = serializers.IntegerField()
    measured = serializers.IntegerField()


class CapturePanelSerializer(_Serializer):
    by_availability_7d = serializers.DictField(child=serializers.IntegerField())
    #: platform → metric → how many measured captures carried a figure.
    coverage_7d = serializers.DictField(child=serializers.DictField(child=CoverageCellSerializer()))
    deferred_rungs = serializers.IntegerField()


class AllowanceSerializer(serializers.Serializer[Any]):
    used = serializers.IntegerField(allow_null=True)
    limit = serializers.IntegerField(allow_null=True)


class AudiencePanelSerializer(_Serializer):
    comments_24h = serializers.IntegerField()
    replies_this_month = serializers.IntegerField()
    moderation_this_month = serializers.IntegerField()
    provider_interactions = AllowanceSerializer()


class RateBudgetPanelSerializer(_Serializer):
    limit_per_minute = serializers.IntegerField()
    publish_reserve_ratio = serializers.FloatField()
    refused_last_hour = serializers.DictField(child=serializers.IntegerField())
    capture_deferrals = serializers.IntegerField()


class CockpitSerializer(serializers.Serializer[Any]):
    generated_at = serializers.DateTimeField()
    provider = ProviderPanelSerializer()
    accounts = AccountsPanelSerializer()
    publishing = PublishingPanelSerializer()
    webhooks = WebhooksPanelSerializer()
    capture = CapturePanelSerializer()
    audience = AudiencePanelSerializer()
    rate_budget = RateBudgetPanelSerializer()
