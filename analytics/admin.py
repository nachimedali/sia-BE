"""Admin for the measurement tables.

Captures are read-only in admin because they are read-only everywhere — the
model guard would refuse a save anyway, and a form that offers a Save button it
cannot honour is worse than no form. `RepurposeConfig` is the one editable row:
§8.9 calls its threshold admin-tunable.
"""

from __future__ import annotations

from django.contrib import admin

from analytics.models import (
    AccountSnapshot,
    AudienceComment,
    MetricCapability,
    PostMetric,
    ProviderCursor,
    RepurposeCandidate,
    RepurposeConfig,
)
from common.admin import ReadOnlyAdmin, all_fields_except_id


@admin.register(PostMetric)
class PostMetricAdmin(ReadOnlyAdmin):
    list_display = (
        "post_target",
        "captured_at",
        "availability",
        "source",
        "impressions",
        "likes",
        "engagement_rate",
    )
    list_filter = ("post_target__platform", "availability", "source", "provider_key")
    readonly_fields = all_fields_except_id(PostMetric)


@admin.register(AudienceComment)
class CommentAdmin(ReadOnlyAdmin):
    list_display = ("author", "sentiment", "availability", "posted_at", "post_target")
    list_filter = ("sentiment", "availability", "post_target__platform")
    search_fields = ("author", "body")
    readonly_fields = all_fields_except_id(AudienceComment)


@admin.register(AccountSnapshot)
class AccountSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("social_account", "captured_at", "followers", "total_posts")
    readonly_fields = all_fields_except_id(AccountSnapshot)


@admin.register(RepurposeCandidate)
class RepurposeCandidateAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("post", "score", "percentile", "reason", "surfaced_at", "dismissed_at")
    list_filter = ("reason",)
    readonly_fields = ("surfaced_at", "created_at", "updated_at")


@admin.register(RepurposeConfig)
class RepurposeConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("percentile_threshold", "updated_at")

    def has_add_permission(self, request: object) -> bool:
        # Singleton: `get_solo()` owns creating it.
        return not RepurposeConfig.objects.exists()

    def has_delete_permission(self, request: object, obj: object = None) -> bool:
        return False


@admin.register(MetricCapability)
class MetricCapabilityAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Editable, unlike the capture tables. C-07's whole point is that
    coverage is operational knowledge that changes when a vendor ships an
    endpoint — a row edit, not a deploy.

    Only `available=False` rows do anything: normalisation reads this as a set
    of *negatives*. A pair with no row is undeclared, not declared-available,
    and the payload settles it.
    """

    list_display = ("provider_key", "platform", "metric_key", "available", "latency_hint")
    list_filter = ("provider_key", "platform", "available")
    search_fields = ("metric_key", "note")


@admin.register(ProviderCursor)
class ProviderCursorAdmin(ReadOnlyAdmin):
    list_display = ("provider_key", "cursor", "bootstrapped_at", "empty_reads", "updated_at")
    readonly_fields = all_fields_except_id(ProviderCursor)
