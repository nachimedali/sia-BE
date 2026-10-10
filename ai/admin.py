"""AI admin (design.md §8.3, I8).

`GenerationCost` and `QualityGateConfig` are the two rows an operator retunes
without a deploy — everything else here is read-only visibility into what the
pipeline actually did.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from ai.models import (
    CreativeOption,
    Generation,
    GenerationCost,
    GenerationVariant,
    QualityCheck,
    QualityGateConfig,
    VoiceProfile,
)


@admin.register(GenerationCost)
class GenerationCostAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("kind", "mode", "provider", "model", "credits", "is_active")
    list_filter = ("kind", "is_active")


@admin.register(QualityGateConfig)
class QualityGateConfigAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("identity_similarity_threshold", "max_regeneration_attempts", "updated_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Singleton (`QualityGateConfig.get_solo`) — a second row would just
        # be dead weight nothing reads.
        return not QualityGateConfig.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class GenerationVariantInline(admin.TabularInline):  # type: ignore[type-arg]
    model = GenerationVariant
    extra = 0
    readonly_fields = ("kind", "body", "media_asset", "rank", "rationale", "was_selected")
    can_delete = False


class QualityCheckInline(admin.TabularInline):  # type: ignore[type-arg]
    model = QualityCheck
    extra = 0
    readonly_fields = ("attempt", "passed", "identity_score", "rejected_reason", "checks")
    can_delete = False


@admin.register(Generation)
class GenerationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "workspace", "kind", "mode", "status", "credits_charged", "created_at")
    list_filter = ("kind", "mode", "status")
    search_fields = ("prompt",)
    inlines = (GenerationVariantInline, QualityCheckInline)

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Every generation goes through the pipeline (entitlements, quality
        # gate, ledger) — an admin-created row would skip all three.
        return False


@admin.register(VoiceProfile)
class VoiceProfileAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "workspace", "updated_at")


@admin.register(CreativeOption)
class CreativeOptionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Where an operator adds a scene or retunes a label — the whole point of
    these being rows. `prompt_fragment` is the sentence the model is given when
    the option is chosen; `metadata` holds per-kind extras (a light's colour
    grade, a format's aspect, a preset's values)."""

    list_display = ("kind", "key", "label", "sort_order", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("key", "label", "description")
    ordering = ("kind", "sort_order")
