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
    CreativeOption,
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationVariant,
    VoiceProfile,
)
from ai.services import creative as creative_service
from ai.services.costing import resolve_pricing, unlock_price
from common.workspaces import scope_related_field_to_workspace
from content.models import MediaAsset
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
            "headline",
            "hashtags",
            "identity_score",
            "media_asset",
            "platform",
            "rank",
            "rationale",
            "was_selected",
            "is_unlocked",
            "unlock_charged",
        )
        read_only_fields = fields


class GenerationSerializer(serializers.ModelSerializer[Generation]):
    variants = GenerationVariantSerializer(many=True, read_only=True)
    #: What one surplus variant costs to keep (X-09). Exposed because the
    #: Studio's retention panel offers the unclaimed variants *before* the
    #: user asks for them, so it needs the price without first provoking the
    #: 402 that used to be the only place it appeared. Resolved off the same
    #: `GenerationCost` row the charge used, never computed by the client —
    #: a panel quoting a figure nothing enforces is Part 7 rule 10's exact
    #: failure, one deploy away from advertising a price the purchase refuses.
    unlock_price = serializers.SerializerMethodField()

    def get_unlock_price(self, obj: Generation) -> int:
        return unlock_price(
            resolve_pricing(kind=obj.kind, mode=obj.mode, provider=obj.provider, model=obj.model)
        )

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
            "creative",
            "motion",
            "duration",
            "is_batch",
            "provider",
            "model",
            "credits_charged",
            "paid_slots",
            "variant_pool",
            "unlock_price",
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
    #: Every Studio control (`CreativeOption` keys, plus the toggles, the
    #: free-text "keep out" and the platforms). Validated against the live
    #: catalog; absent for every non-Studio caller.
    creative = serializers.JSONField(required=False, default=dict)
    is_batch = serializers.BooleanField(default=False)
    n = serializers.IntegerField(default=3, min_value=1, max_value=6)
    #: **How many variants the buyer keeps** (X-09). Absent means the caller
    #: wants pre-X-09 terms — one charge, no surplus, no dock — which is what
    #: every non-Studio caller wants and what the flag collapses to. Present
    #: means slot pricing: `paid_slots` x the per-variant price, a pool
    #: rendered around it, and the rest buyable at `unlock_percent`.
    paid_slots = serializers.IntegerField(required=False, min_value=1, max_value=6)
    #: `CAPTION` only — the image to read (P1-13). Scoped to the caller's own
    #: workspace like every other reference on this serializer.
    source_media = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.none(), required=False, allow_null=True
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        scope_related_field_to_workspace(self.fields["product"], request, Product)
        scope_related_field_to_workspace(self.fields["voice_profile"], request, VoiceProfile)
        scope_related_field_to_workspace(self.fields["source_media"], request, MediaAsset)

    def validate_creative(self, value: Any) -> dict[str, Any]:
        return creative_service.normalize(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """The first of I5's four gates (design.md §8.1) — credits are
        re-checked independently by `ai.permissions.HasSufficientCredits`,
        `ai.services.pipeline.create_generation`, and surfaced at
        `GET /billing/entitlements/`; none of the three relies on this one
        having run."""
        from ai.services.costing import preflight_require_credits
        from common.workspaces import request_workspace

        if attrs["mode"] == GenerationMode.CAPTION and not attrs.get("source_media"):
            raise serializers.ValidationError({"source_media": "A caption needs an image to read."})

        # **The format owns the kind and the aspect.** A "Reel 9:16" that
        # quietly rendered a 1:1 text post would be a control that changed
        # nothing, so a mismatch is refused rather than reconciled.
        chosen = (attrs.get("creative") or {}).get("format")
        if chosen:
            row = CreativeOption.objects.filter(kind="format", key=chosen).first()
            if row is not None:
                if row.metadata.get("kind") != attrs["kind"]:
                    raise serializers.ValidationError(
                        {
                            "creative": {
                                "format": f"'{chosen}' is a {row.metadata.get('kind')} format."
                            }
                        }
                    )
                attrs["aspect"] = row.metadata.get("aspect", attrs["aspect"])

        request = self.context.get("request")
        if request is not None:
            preflight_require_credits(
                request_workspace(request), kind=attrs["kind"], mode=attrs["mode"]
            )
        return attrs


class ReviseRequestSerializer(serializers.Serializer[Any]):
    instructions = serializers.CharField()
    n = serializers.IntegerField(default=1, min_value=1, max_value=6)


class RankedHashtagSerializer(serializers.Serializer[Any]):
    tag = serializers.CharField()
    count = serializers.IntegerField()


class HashtagSuggestionSerializer(serializers.Serializer[Any]):
    hashtags = RankedHashtagSerializer(many=True)


class VariantSelectionSerializer(serializers.Serializer[object]):
    """The dock's whole answer, not one click (X-09).

    A replace rather than a toggle: sending the complete set removes any
    question about what happened to a click that did not arrive.
    """

    variant_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=True, max_length=12
    )


class VariantCommitSerializer(serializers.Serializer[object]):
    """`scheduled_at` optional: a selection with no time lands as a draft on
    the calendar, which is where a user who has not decided when wants it.
    Given a time, `schedule_post` applies the horizon, quota and approval
    gates exactly as a hand-typed post's would be."""

    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)


class EditVariantRequestSerializer(serializers.Serializer[Any]):
    """Edit one variant's copy before it is sent. Either field may be omitted:
    changing the headline must not blank the caption."""

    variant = serializers.IntegerField()
    headline = serializers.CharField(required=False, allow_blank=True, max_length=200)
    body = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "headline" not in attrs and "body" not in attrs:
            raise serializers.ValidationError("Send a headline, a body, or both.")
        return attrs


class CreativeOptionSerializer(serializers.ModelSerializer[CreativeOption]):
    """What a control needs to draw itself. `prompt_fragment` stays server-side:
    it is instruction to a model, not content for a tile."""

    class Meta:
        model = CreativeOption
        fields: ClassVar[tuple[str, ...]] = (
            "key",
            "label",
            "description",
            "icon_paths",
            "colors",
            "metadata",
        )
        read_only_fields = fields


class CreativePricingSerializer(serializers.Serializer[Any]):
    credits = serializers.IntegerField()
    variant_pool = serializers.IntegerField()
    unlock_price = serializers.IntegerField()


class CreativeCatalogSerializer(serializers.Serializer[Any]):
    options = serializers.DictField(child=CreativeOptionSerializer(many=True))
    #: Per generation kind, from the cost table — `null` for a kind with no
    #: row, never zero (a price we do not know is not a price of nothing).
    pricing = serializers.DictField(child=CreativePricingSerializer(allow_null=True))
