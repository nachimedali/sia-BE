"""Benchmark admin.

`ConsentPolicy` is the one place legal's terms enter the system, so it can be
added and never changed: a published policy is what existing agreements cite.
`BenchmarkConfig` is the editable singleton. Consent records and published runs
are evidence and are read-only. The projection is not registered — it is a
derived table, and a hand edit there would be overwritten by the next run.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from benchmarks.models import (
    BenchmarkConfig,
    BenchmarkRun,
    CohortBenchmark,
    ConsentPolicy,
    ConsentRecord,
)
from common.admin import ReadOnlyAdmin, all_fields_except_id


@admin.register(ConsentPolicy)
class ConsentPolicyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("version", "published_at", "document_url")

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return obj is None

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdmin):
    list_display = ("workspace", "action", "policy", "actor", "recorded_at")
    list_filter = ("action", "policy")
    readonly_fields = all_fields_except_id(ConsentRecord)


@admin.register(BenchmarkConfig)
class BenchmarkConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("min_workspaces", "min_posts", "max_posts_per_contributor", "updated_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not BenchmarkConfig.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(BenchmarkRun)
class BenchmarkRunAdmin(ReadOnlyAdmin):
    list_display = ("computed_at", "window_start", "window_end", "min_workspaces", "min_posts")
    readonly_fields = all_fields_except_id(BenchmarkRun)


@admin.register(CohortBenchmark)
class CohortBenchmarkAdmin(ReadOnlyAdmin):
    list_display = (
        "run",
        "vertical",
        "market",
        "platform",
        "size_band",
        "post_format",
        "sufficient",
    )
    list_filter = ("sufficient", "platform", "market")
    readonly_fields = all_fields_except_id(CohortBenchmark)
