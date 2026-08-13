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
    Comment,
    PostMetric,
    RepurposeCandidate,
    RepurposeConfig,
)
from common.admin import ReadOnlyAdmin, all_fields_except_id


@admin.register(PostMetric)
class PostMetricAdmin(ReadOnlyAdmin):
    list_display = ("post_target", "captured_at", "impressions", "likes", "engagement_rate")
    list_filter = ("post_target__platform",)
    readonly_fields = all_fields_except_id(PostMetric)


@admin.register(Comment)
class CommentAdmin(ReadOnlyAdmin):
    list_display = ("author", "sentiment", "posted_at", "post_target")
    list_filter = ("sentiment", "post_target__platform")
    search_fields = ("author", "body")
    readonly_fields = all_fields_except_id(Comment)


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
