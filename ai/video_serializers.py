"""Shapes for the video endpoints (steps-plan S3). Type checks only — what a
key *means* (a catalogue row, this workspace's still) is `ai.services.video`'s
to decide."""

from __future__ import annotations

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ai.models import VideoMode, VideoRender, VideoReview
from ai.serializers import CreativeOptionSerializer
from ai.services.video import OVERLAY_MAX, TEXT_MAX
from content.models import MediaAsset
from content.serializers import MediaAssetSerializer


class VideoCatalogueSerializer(serializers.Serializer[Any]):
    lengths = CreativeOptionSerializer(many=True)
    motions = CreativeOptionSerializer(many=True)
    aspects = CreativeOptionSerializer(many=True)
    reel_styles = CreativeOptionSerializer(many=True)
    music = CreativeOptionSerializer(many=True)
    extras = CreativeOptionSerializer(many=True)
    #: When the catalogue last changed (ISO 8601), or "" when it is empty.
    version = serializers.CharField()


class VideoShotSerializer(serializers.Serializer[Any]):
    media = serializers.IntegerField()
    text = serializers.CharField(
        max_length=OVERLAY_MAX, allow_blank=True, required=False, default=""
    )


class VideoRequestSerializer(serializers.Serializer[Any]):
    """One request, priced or rendered. A clip reads `source`, `role`,
    `motion` and `aspect`; a reel reads `shots`, `style`, `music`, `captions`,
    `overlays`, `end_card` and `cta` (reels are always 9:16)."""

    mode = serializers.ChoiceField(choices=VideoMode.choices)
    length = serializers.CharField(max_length=40)
    text = serializers.CharField(max_length=TEXT_MAX, allow_blank=True, required=False, default="")
    product = serializers.IntegerField(required=False, allow_null=True, default=None)
    language = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    # clip
    source = serializers.IntegerField(required=False, allow_null=True, default=None)  # type: ignore[assignment]
    role = serializers.ChoiceField(choices=["start", "end"], required=False, default="start")
    motion = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    aspect = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    # reel
    shots = VideoShotSerializer(many=True, required=False, default=list)
    style = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")  # type: ignore[assignment]
    music = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    captions = serializers.BooleanField(required=False, default=False)
    overlays = serializers.BooleanField(required=False, default=False)
    end_card = serializers.BooleanField(required=False, default=False)
    cta = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")


class VideoRenderCreateSerializer(VideoRequestSerializer):
    #: The estimate the person confirmed. The price is re-worked and must
    #: match, or the request is a 409 carrying the new estimate.
    estimate_id = serializers.CharField(max_length=32)
    #: A failed render this one replaces; it is marked retried.
    retry_of = serializers.IntegerField(required=False, allow_null=True, default=None)
    #: The post the clip is for (the post editor's Animate). It is staged on
    #: that post by the editor and never sent anywhere else.
    post = serializers.IntegerField(required=False, allow_null=True, default=None)


class VideoEstimateLineSerializer(serializers.Serializer[Any]):
    label = serializers.CharField()  # type: ignore[assignment]
    credits = serializers.IntegerField()


class VideoEstimateSerializer(serializers.Serializer[Any]):
    lines = VideoEstimateLineSerializer(many=True)
    credits = serializers.IntegerField()
    render_s = serializers.IntegerField()
    estimate_id = serializers.CharField()


class VideoStillSerializer(serializers.Serializer[Any]):
    """A still the render was made from, for drawing it while it renders."""

    id = serializers.IntegerField()
    url = serializers.CharField(allow_null=True)
    text = serializers.CharField(allow_blank=True)


class VideoRenderSerializer(serializers.ModelSerializer[VideoRender]):
    output = MediaAssetSerializer(read_only=True, allow_null=True)
    lines = VideoEstimateLineSerializer(many=True, read_only=True)
    stills = serializers.SerializerMethodField()
    queue_position = serializers.SerializerMethodField()
    #: What the ledger actually took: the price once it passed, 0 before and
    #: on failure. The hold is `credits` while the render is in flight.
    charged = serializers.SerializerMethodField()

    class Meta:
        model = VideoRender
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "mode",
            "status",
            "phase",
            "progress",
            "review",
            "credits",
            "charged",
            "lines",
            "render_s",
            "spec",
            "text",
            "product",
            "stills",
            "output",
            "error",
            "error_code",
            "post",
            "retry_of",
            "queue_position",
            "provider",
            "created_at",
            "started_at",
            "finished_at",
        )
        read_only_fields = fields

    @extend_schema_field(VideoStillSerializer(many=True))
    def get_stills(self, obj: VideoRender) -> list[dict[str, Any]]:
        assets: dict[int, MediaAsset] = self.context.get("assets", {})
        if obj.mode == VideoMode.CLIP:
            wanted = [(obj.spec.get("source"), "")]
        else:
            wanted = [
                (shot.get("media"), shot.get("text", "")) for shot in obj.spec.get("shots", [])
            ]
        out = []
        for media_id, text in wanted:
            asset = assets.get(media_id) if media_id is not None else None
            if asset is None and obj.source_id == media_id:
                asset = obj.source
            out.append(
                {
                    "id": media_id,
                    "url": asset.file.url if asset and asset.file else None,
                    "text": text,
                }
            )
        return out

    def get_queue_position(self, obj: VideoRender) -> int | None:
        positions: dict[int, int] = self.context.get("positions", {})
        return positions.get(obj.pk)

    def get_charged(self, obj: VideoRender) -> int:
        return obj.credits if obj.charge_id is not None else 0


class VideoCreditsSerializer(serializers.Serializer[Any]):
    #: The ledger balance — null on an unlimited plan, never a made-up number.
    balance = serializers.IntegerField(allow_null=True)
    held = serializers.IntegerField()


class VideoRenderListSerializer(serializers.Serializer[Any]):
    renders = VideoRenderSerializer(many=True)
    credits = VideoCreditsSerializer()


class VideoReviewSerializer(serializers.Serializer[Any]):
    #: Blank undoes an accept or a discard.
    review = serializers.ChoiceField(
        choices=[
            ("", "None"),
            *[(key, name) for key, name in VideoReview.choices if key != VideoReview.RETRIED],
        ],
        allow_blank=True,
    )


class VideoSendSerializer(serializers.Serializer[Any]):
    renders = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=24)
    platforms = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False, default=list
    )


class VideoSendResponseSerializer(serializers.Serializer[Any]):
    posts = serializers.ListField(child=serializers.IntegerField())
