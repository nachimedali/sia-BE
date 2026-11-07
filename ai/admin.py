"""AI admin (design.md §8.3, I8).

`GenerationCost` and `QualityGateConfig` are the two rows an operator retunes
without a deploy — everything else here is read-only visibility into what the
pipeline actually did.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db import transaction
from django.http import HttpRequest
from django.utils.html import format_html, format_html_join

from ai.models import (
    AIKind,
    AIModelRoute,
    AIModelRouteChange,
    CreativeOption,
    Generation,
    GenerationCost,
    GenerationVariant,
    QualityCheck,
    QualityGateConfig,
    VideoRender,
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


def _thumbnail(url: str) -> str:
    return format_html(
        '<a href="{}" target="_blank"><img src="{}" style="max-height:160px;max-width:160px"></a>',
        url,
        url,
    )


class QualityCheckInline(admin.TabularInline):  # type: ignore[type-arg]
    """Every attempt the gate judged, rejected ones included, with the picture
    it was shown — the one place a refused image can be seen."""

    model = QualityCheck
    extra = 0
    fields = (
        "attempt",
        "picture",
        "passed",
        "identity_score",
        "rejected_reason",
        "provider",
        "model",
        "checks",
        "prompt_sent",
    )
    readonly_fields = fields
    can_delete = False

    @admin.display(description="picture")
    def picture(self, obj: QualityCheck) -> str:
        return _thumbnail(obj.image.url) if obj.image else "—"


@admin.register(Generation)
class GenerationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "workspace", "kind", "mode", "status", "credits_charged", "created_at")
    list_filter = ("kind", "mode", "status")
    search_fields = ("prompt",)
    inlines = (GenerationVariantInline, QualityCheckInline)
    readonly_fields = ("product_photos",)

    @admin.display(description="product photos the gate compares against")
    def product_photos(self, obj: Generation) -> str:
        if obj.product is None:
            return "—"
        return (
            format_html_join(
                " ",
                "{}",
                ((_thumbnail(asset.file.url),) for asset in obj.product.reference_images.all()),
            )
            or "—"
        )

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


class AIModelRouteChangeInline(admin.TabularInline):  # type: ignore[type-arg]
    model = AIModelRouteChange
    extra = 0
    can_delete = False
    readonly_fields = ("created_at", "actor", "before", "after")
    fields = readonly_fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(AIModelRoute)
class AIModelRouteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Which model each task runs on, per package (`ai.services.routing`).

    A row with no plan is the default every package inherits; a row with a
    plan overrides it for that plan. The form refuses a provider this
    deployment does not point the task's port at, and any parameter the
    task's kind does not take (`AIModelRoute.clean`). Every save is recorded
    below the form. Deactivate a row rather than delete it: its history is
    kept, and the task falls back to the default row, then to settings.
    """

    list_display = (
        "task",
        "scope",
        "provider",
        "model",
        "fallback_model",
        "params",
        "is_active",
        "updated_at",
    )
    list_filter = ("task", "plan", "provider", "is_active")
    search_fields = ("model", "fallback_model", "note")
    ordering = ("task", "plan__sort_order")
    readonly_fields = ("charged_at", "updated_by", "created_at", "updated_at")
    fields = (
        "task",
        "plan",
        "provider",
        "model",
        "fallback_model",
        "params",
        "is_active",
        "note",
        "charged_at",
        "updated_by",
        "created_at",
        "updated_at",
    )
    inlines = (AIModelRouteChangeInline,)

    @admin.display(description="Package")
    def scope(self, obj: AIModelRoute) -> str:
        return obj.plan.display_name if obj.plan is not None else "Default (every package)"

    @admin.display(description="Charged at")
    def charged_at(self, obj: AIModelRoute) -> str:
        """Which `GenerationCost` row a generation on this model is priced by.
        A model change does not re-price anything by itself; this says so."""
        if not obj.pk or not obj.task:
            return "—"
        if obj.kind == AIKind.VIDEO:
            return "Video is priced by the length rows (Creative options → video lengths)."
        kind = obj.kind.upper()  # AIKind and GenerationKind share their values
        exact = GenerationCost.objects.filter(
            kind=kind, provider=obj.provider, model=obj.model, is_active=True
        ).first()
        if exact is not None:
            return f"Its own row: {exact}"
        return f"The generic {kind} rows — no cost row names {obj.provider}/{obj.model}."

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return obj is None or not obj.changes.exists()

    @transaction.atomic
    def save_model(self, request: HttpRequest, obj: AIModelRoute, form: Any, change: bool) -> None:
        before = AIModelRoute.objects.get(pk=obj.pk).snapshot() if change and obj.pk else {}
        obj.updated_by = request.user if request.user.is_authenticated else None
        super().save_model(request, obj, form, change)
        AIModelRouteChange.objects.create(
            route=obj,
            actor=obj.updated_by,
            before=before,
            after=obj.snapshot(),
        )


@admin.register(VideoRender)
class VideoRenderAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only: what each render sent the video model and how it ended. Its
    first frame is a `Generation` (`keyframe`), with every gate attempt."""

    list_display = ("id", "workspace", "mode", "status", "error_code", "created_at")
    list_filter = ("mode", "status")
    fields = (
        "workspace",
        "product",
        "mode",
        "status",
        "phase",
        "error",
        "error_code",
        "keyframe",
        "source",
        "output",
        "prompt_sent",
        "rephrased_prompt",
        "rephrased_by",
        "spec",
        "credits",
        "units",
        "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
