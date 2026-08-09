"""Generation serialisation (design.md §7).

`GenerateRequestSerializer` expands design.md's `{kind, mode, prompt,
product, params, n}` request shape into explicit fields (`aspect`,
`render_style`, `scene`, `is_batch`) rather than an opaque `params` blob —
consistent with how every other serializer in this codebase declares its
write surface, and it is what gives the OpenAPI schema (and the generated FE
client) real field names instead of an untyped dict (design.md §15.8 A74).
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from ai.models import (
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationVariant,
    VoiceProfile,
)
from common.workspaces import scope_related_field_to_workspace
from content.serializers import MediaAssetSerializer
from products.models import Product


class VoiceProfileSerializer(serializers.ModelSerializer[VoiceProfile]):
    class Meta:
        model = VoiceProfile
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "tone_descriptors",
            "banned_phrases",
            "exemplar_post_ids",
            "system_prompt",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class GenerationVariantSerializer(serializers.ModelSerializer[GenerationVariant]):
    # allow_null=True: a TEXT variant's media_asset is genuinely None. DRF
    # serialises that correctly either way, but without this the generated
    # OpenAPI schema (and so the FE client) claims the field is never null.
    media_asset = MediaAssetSerializer(read_only=True, allow_null=True)

    class Meta:
        model = GenerationVariant
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "kind",
            "body",
            "media_asset",
            "platform",
            "rank",
            "rationale",
            "was_selected",
        )
        read_only_fields = fields


class GenerationSerializer(serializers.ModelSerializer[Generation]):
    variants = GenerationVariantSerializer(many=True, read_only=True)

    class Meta:
        model = Generation
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "kind",
            "mode",
            "prompt",
            "product",
            "category",
            "voice_profile",
            "parent_generation",
            "output_type",
            "aspect",
            "render_style",
            "scene",
            "motion",
            "duration",
            "is_batch",
            "provider",
            "model",
            "credits_charged",
            "video_units_charged",
            "latency_ms",
            "status",
            "error_detail",
            "variants",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class GenerateRequestSerializer(serializers.Serializer[Any]):
    """`product`/`voice_profile` start scoped to nothing — `__init__` narrows
    each queryset to the caller's own workspace before validating, the same
    reason `content/serializers.py::_scope_media_field` exists: a bare
    model-wide queryset would let one workspace reference another's product
    or voice profile."""

    kind = serializers.ChoiceField(choices=GenerationKind.choices)
    mode = serializers.ChoiceField(choices=GenerationMode.choices)
    prompt = serializers.CharField(allow_blank=True, default="")
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.none(), required=False, allow_null=True
    )
    voice_profile = serializers.PrimaryKeyRelatedField(
        queryset=VoiceProfile.objects.none(), required=False, allow_null=True
    )
    aspect = serializers.CharField(default="1:1")
    render_style = serializers.CharField(allow_blank=True, default="")
    scene = serializers.CharField(allow_blank=True, default="")
    is_batch = serializers.BooleanField(default=False)
    n = serializers.IntegerField(default=3, min_value=1, max_value=6)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        scope_related_field_to_workspace(self.fields["product"], request, Product)
        scope_related_field_to_workspace(self.fields["voice_profile"], request, VoiceProfile)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """The first of I5's four gates (design.md §8.1) — credits are
        re-checked independently by `ai.permissions.HasSufficientCredits`,
        `ai.services.pipeline.create_generation`, and surfaced at
        `GET /billing/entitlements/`; none of the three relies on this one
        having run."""
        from ai.services.costing import preflight_require_credits
        from common.workspaces import active_workspace

        request = self.context.get("request")
        if request is not None:
            preflight_require_credits(
                active_workspace(request), kind=attrs["kind"], mode=attrs["mode"]
            )
        return attrs


class ReviseRequestSerializer(serializers.Serializer[Any]):
    instructions = serializers.CharField()
    n = serializers.IntegerField(default=1, min_value=1, max_value=6)
