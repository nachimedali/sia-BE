"""Video renders: the Studio's Motion step (steps-plan S3).

```
estimate        spec → priced lines + an estimate id      (nothing is held)
start_render    re-price, compare ids, HOLD the credits   → QUEUED, task queued
run_render      stills → provider/composer → quality gate
                  pass → debit once, ingest the file      → SUCCEEDED
                  fail → nothing written to the ledger    → FAILED (hold released)
review          accept · discard · hide · undo
send            accepted renders → draft posts            → the normal approval path
```

**Two kinds of render.** A *clip* animates one still for 5 or 10 seconds,
opening on it (`start`) or landing on it (`end`) — `VideoProvider.animate`. A
*reel* cuts 3 to 5 stills into a 9:16 master with a line over each shot, a music
bed, captions burned in from the post text and an end card —
`VideoComposer.compose`. Both are ports with fakes (Part 7 rule 6).

**Prices are rows.** Every figure comes from a `CreativeOption` in the video
kinds (`ai/video_seed.py`); a priced row with no `metadata.credits` is a
configuration error, never a free render.

**The hold.** Confirming an estimate holds its price: a render in flight counts
against what the workspace can spend, so two renders cannot both be promised
the same credits. The hold is `held_credits()` — a sum over in-flight rows,
computed every time, never cached — so it needs no ledger row to create and
none to release. The ledger is written exactly once, when a render passes, in
the same transaction that marks it `SUCCEEDED`; a retried task finds it no
longer `RUNNING` and stops. That is S3's gate: debited once, and only on pass.

**Nothing publishes from here.** `send` makes ordinary draft posts through
`create_post`; scheduling and approval are the post's own path (L-2).

**S17 — templates and video units** (`ad_templates_s17`). A clip may name an ad
template (`ai.services.ad_templates`): its motion comes from the template, its
first frame is the still the person picked or one this render makes first
(`keyframe`, an image generation paid in credits on pass), and an avatar puts a
person on screen — labelled AI-generated through to publishing. With the flag
on, every render that is not on the free allowance is priced in **video
units** (`metadata.units` of its length or template) and paid from
`VideoLedger`: held while in flight (`held_units`), debited once on pass. With
the flag off, nothing here changes.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import NotFound, ValidationError

from accounts.models import User
from ai.models import (
    CreativeKind,
    CreativeOption,
    VideoMode,
    VideoRender,
    VideoRenderStatus,
    VideoReview,
)
from ai.providers.base import CaptionCue, EndCard, ReelShot, VideoResult
from ai.providers.video import get_video_composer, get_video_provider
from ai.services import ad_templates, disclosure, graphics, quality, render_checks, rephrase
from ai.services import creative as creative_service
from ai.services.creative import VIDEO_KINDS
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from billing.services.flags import STUDIO_REPHRASE, flag_enabled
from billing.services.plans import suggest_upgrade
from brand.services.kit import kit_for
from common.exceptions import (
    FeatureNotAvailable,
    InsufficientCredits,
    InsufficientVideoUnits,
    OCCSError,
    ProviderError,
    StateConflict,
)
from content.models import MediaAsset, MediaKind, MediaSource, Post, PostSource
from content.services.media import ingest_media
from content.services.posts import create_post, update_post
from products.brief import identity_photos
from products.models import Product
from workspaces.models import Organization, Workspace

logger = logging.getLogger(__name__)

#: What the render queue shows while a render runs, and how far along each
#: phase puts it. Written to the row as the work actually reaches each step.
PHASES: tuple[tuple[str, float], ...] = (
    ("KEYFRAMES", 0.15),
    ("MOTION", 0.4),
    ("UPSCALE", 0.75),
    ("QUALITY CHECK", 0.9),
)
#: Burned-in captions: a phone screen holds about five words at a readable
#: size, and more than eight cards in a reel reads as a transcript.
CAPTION_WORDS = 5
CAPTION_MAX_CUES = 8
OVERLAY_MAX = 60
TEXT_MAX = 2200
#: The reel frame. S3 renders reels as a vertical master.
REEL_ASPECT = "9:16"
#: How much of a reel the end card takes, when it is on.
END_CARD_SECONDS = 2.0
#: The end card without a brand kit: the dark card and white pill it had.
END_CARD_BACKGROUND = "#111111"
END_CARD_ACCENT = "#FFFFFF"
#: A logo smaller than this is a favicon: never stretched onto a card
#: (goose-skills `review-finished-ad`: long side under 256 px, or 40,000 px²).
LOGO_MIN_SIDE = 256
LOGO_MIN_AREA = 40_000
#: A single-clip template's words on screen (S17-26): the hook opens it, the
#: call to action closes it, each for at most this long; the body has the rest.
BEAT_EDGE_SECONDS = 3.0
#: Seeds sent to the video vendors: a non-negative 31-bit integer every
#: vendor that takes one accepts.
SEED_RANGE = 2**31
ACTIVE = (VideoRenderStatus.QUEUED, VideoRenderStatus.RUNNING)


class VideoUnavailableError(OCCSError):
    status_code = 422
    default_code = "video_unavailable"
    default_detail = "Video rendering is not available on this deployment."


class VideoPriceNotConfiguredError(OCCSError):
    default_code = "video_price_not_configured"
    default_detail = "A price is missing from the video catalogue."


class EstimateChangedError(StateConflict):
    default_code = "estimate_changed"
    default_detail = "The price changed since you confirmed it. Review the new estimate."


class RenderNotReviewableError(StateConflict):
    default_code = "render_not_reviewable"
    default_detail = "This render cannot be changed that way now."


# -----------------------------------------------------------------------------
# The catalogue
# -----------------------------------------------------------------------------


def catalogue() -> dict[str, list[CreativeOption]]:
    """Every active video row, by kind, in display order."""
    grouped: dict[str, list[CreativeOption]] = {kind.value: [] for kind in sorted(VIDEO_KINDS)}
    for row in CreativeOption.objects.filter(kind__in=grouped, is_active=True):
        grouped[row.kind].append(row)
    return grouped


def catalogue_version(rows: dict[str, list[CreativeOption]]) -> str:
    """When the catalogue last changed — shown under the controls, so a price
    that moved has a visible reason."""
    stamps = [row.updated_at for kind in rows.values() for row in kind]
    return max(stamps).isoformat() if stamps else ""


def units_of(row: CreativeOption) -> int:
    value = (row.metadata or {}).get("units")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise VideoPriceNotConfiguredError(
            detail={"kind": row.kind, "key": row.key, "price": "units"}
        )
    return value


def credits_of(row: CreativeOption) -> int:
    value = (row.metadata or {}).get("credits")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VideoPriceNotConfiguredError(detail={"kind": row.kind, "key": row.key})
    return value


# -----------------------------------------------------------------------------
# The request, normalised
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Shot:
    media: MediaAsset
    text: str


@dataclass(frozen=True)
class Spec:
    mode: str
    length: CreativeOption
    aspect: CreativeOption
    text: str
    product: Product | None
    language: str
    # clip
    source: MediaAsset | None = None
    motion: CreativeOption | None = None
    role: str = "start"
    # reel
    shots: tuple[Shot, ...] = ()
    style: CreativeOption | None = None
    music: CreativeOption | None = None
    captions: bool = False
    overlays: bool = False
    end_card: bool = False
    cta: CreativeOption | None = None
    extras: dict[str, CreativeOption] = field(default_factory=dict)
    creative: dict[str, Any] = field(default_factory=dict)
    # S17: an ad template, the person on screen and where it is set.
    template: CreativeOption | None = None
    avatar: Any = None
    extra_avatars: list[Any] = field(default_factory=list)
    extra_products: list[Product] = field(default_factory=list)
    location: str = ""
    script: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    #: An offer template's real offer (S17-33), `{text, ends}`.
    offer: dict[str, str] = field(default_factory=dict)
    #: The script's angle and hook style (S17-37), kept for results.
    angle: str = ""
    family: str = ""

    @property
    def seconds(self) -> int:
        return int(self.length.metadata["seconds"])

    @property
    def aspect_ratio(self) -> str:
        return str(self.aspect.metadata["aspect"])

    def stored(self) -> dict[str, Any]:
        """Keys and ids only — what `VideoRender.spec` keeps."""
        out: dict[str, Any] = {
            "length": self.length.key,
            "seconds": self.seconds,
            "aspect": self.aspect.key,
            "language": self.language,
        }
        if self.template is not None:
            out |= {
                "template": self.template.key,
                "source": self.source.pk if self.source else None,
                "avatar": self.avatar.pk if self.avatar else None,
                "extra_avatars": [item.pk for item in self.extra_avatars],
                "extra_products": [item.pk for item in self.extra_products],
                "location": self.location,
                "role": self.role,
                "script": self.script,
                **({"prompt": self.prompt} if self.prompt else {}),
                **({"offer": self.offer} if self.offer else {}),
                **({"angle": self.angle} if self.angle else {}),
                **({"family": self.family} if self.family else {}),
            }
        elif self.mode == VideoMode.CLIP:
            assert self.source is not None and self.motion is not None
            out |= {"source": self.source.pk, "motion": self.motion.key, "role": self.role}
        else:
            assert self.style is not None and self.music is not None
            out |= {
                "shots": [{"media": shot.media.pk, "text": shot.text} for shot in self.shots],
                "style": self.style.key,
                "music": self.music.key,
                "captions": self.captions,
                "overlays": self.overlays,
                "end_card": self.end_card,
                "cta": self.cta.key if self.cta else "",
            }
        if self.creative:
            out["creative"] = self.creative
        return out


def _pick(kind: str, key: Any, field_name: str, *, required: bool = True) -> CreativeOption | None:
    if key in (None, ""):
        if required:
            raise ValidationError({field_name: "This field is required."})
        return None
    row = CreativeOption.objects.filter(kind=kind, key=str(key), is_active=True).first()
    if row is None:
        raise ValidationError({field_name: f"'{key}' is not in the video catalogue."})
    return row


def _media(workspace: Workspace, media_id: Any) -> MediaAsset:
    """The workspace's own still, or 404 — another tenant's id is not a
    validation error, it is a thing that does not exist here (rule 3)."""
    asset = MediaAsset.objects.filter(workspace=workspace, pk=media_id).first()
    if asset is None:
        raise NotFound("That image is not in this workspace's library.")
    if asset.kind != MediaKind.IMAGE:
        raise ValidationError({"source": "Only an image can be animated."})
    return asset


def parse(workspace: Workspace, data: dict[str, Any]) -> Spec:
    """Validate a request against the live catalogue and the workspace.

    `data` has been type-checked by the serializer; this checks *meaning*:
    the keys are rows, the stills are this workspace's, the shot count is one
    the length accepts. An unknown key is a 400, never ignored (rule 18).
    """
    mode = data["mode"]
    length = _pick(CreativeKind.VIDEO_LENGTH, data.get("length"), "length")
    assert length is not None
    if (length.metadata or {}).get("mode") != mode:
        raise ValidationError({"length": f"'{length.key}' is not a {mode} length."})

    product = None
    if data.get("product") is not None:
        product = Product.objects.filter(workspace=workspace, pk=data["product"]).first()
        if product is None:
            raise NotFound("That product is not in this workspace.")

    language = str(data.get("language") or "")
    if language:
        _pick(CreativeKind.LANGUAGE, language, "language")

    text = str(data.get("text") or "").strip()
    common: dict[str, Any] = {
        "mode": mode,
        "length": length,
        "text": text,
        "product": product,
        "creative": data.get("creative") or {},
    }

    if data.get("template"):
        return _parse_template(workspace, data, common, language=language)

    if mode == VideoMode.CLIP:
        aspect = _pick(CreativeKind.VIDEO_ASPECT, data.get("aspect"), "aspect")
        motion = _pick(CreativeKind.MOTION, data.get("motion"), "motion")
        if data.get("source") is None:
            raise ValidationError({"source": "Pick the image to animate."})
        assert aspect is not None
        return Spec(
            **common,
            aspect=aspect,
            language=language,
            source=_media(workspace, data["source"]),
            motion=motion,
            role=data.get("role") or "start",
        )

    aspect = _vertical()
    shots_in = data.get("shots") or []
    low = int(length.metadata.get("min_shots", 1))
    high = int(length.metadata.get("max_shots", low))
    if not low <= len(shots_in) <= high:
        raise ValidationError({"shots": f"A reel needs {low} to {high} shots."})
    shots = tuple(
        Shot(media=_media(workspace, shot["media"]), text=str(shot.get("text") or "").strip())
        for shot in shots_in
    )
    captions = bool(data.get("captions"))
    if captions and not text:
        raise ValidationError({"text": "Captions are written from the post text. Add some first."})
    extras = {
        row.key: row
        for row in CreativeOption.objects.filter(kind=CreativeKind.VIDEO_EXTRA, is_active=True)
    }
    if "shot" not in extras or (captions and "captions" not in extras):
        raise VideoPriceNotConfiguredError(detail={"kind": CreativeKind.VIDEO_EXTRA})
    return Spec(
        **common,
        aspect=aspect,
        language=language,
        shots=shots,
        style=_pick(CreativeKind.REEL_STYLE, data.get("style"), "style"),
        music=_pick(CreativeKind.MUSIC, data.get("music"), "music"),
        captions=captions,
        overlays=bool(data.get("overlays")),
        end_card=bool(data.get("end_card")),
        cta=_pick(CreativeKind.CTA, data.get("cta"), "cta", required=False),
        extras=extras,
    )


def _directed(lead: str, render: VideoRender) -> str:
    """`lead` plus the look the person chose and their own words."""
    return " ".join(
        filter(
            None,
            [
                lead,
                *creative_service.image_lines(render.spec.get("creative") or {}),
                render.spec.get("prompt", ""),
            ],
        )
    )


def _key(kind: str, key: Any, field_name: str) -> str:
    """A catalogue key the request named, or ""; an unknown one is a 400."""
    row = _pick(kind, key, field_name, required=False)
    return row.key if row is not None else ""


def _vertical() -> CreativeOption:
    aspect = CreativeOption.objects.filter(
        kind=CreativeKind.VIDEO_ASPECT, is_active=True, metadata__aspect=REEL_ASPECT
    ).first()
    if aspect is None:
        raise VideoPriceNotConfiguredError(
            "The 9:16 frame is missing from the video catalogue.",
            detail={"kind": CreativeKind.VIDEO_ASPECT, "aspect": REEL_ASPECT},
        )
    return aspect


def _parse_template(
    workspace: Workspace, data: dict[str, Any], common: dict[str, Any], *, language: str
) -> Spec:
    """A clip made from an ad template: always 9:16, always opening on its
    first frame, the motion the template's own. The template, the avatar and
    the plan are checked here, so the estimate already says why it cannot be
    made rather than the render failing later."""
    ad_templates.require_on(workspace)
    if data["mode"] != VideoMode.CLIP:
        raise ValidationError({"mode": "A template renders as a clip."})
    product = common["product"]
    row, avatar = ad_templates.resolve_with_avatar(
        workspace,
        data["template"],
        product=product,
        avatar_id=data.get("avatar"),
        language=language,
    )
    assert product is not None
    people, others = ad_templates.resolve_cast(
        workspace,
        avatar=avatar,
        product=product,
        extra_avatars=list(data.get("extra_avatars") or []),
        extra_products=list(data.get("extra_products") or []),
    )
    length = common["length"]
    ad_templates.length_units(row, length.key)
    location = str(data.get("location") or "")
    # 400 on a bad location, 404 on another brand's own place.
    ad_templates.keyframe_prompt(row, product=product, location=location, workspace=workspace)
    # A template made only from the product's photos has no first frame to pick.
    source = (
        _media(workspace, data["source"])
        if data.get("source") is not None and ad_templates.has_frame(row)
        else None
    )
    assert product is not None
    script = ad_templates.script_parts(row, data.get("script"), product)
    # A real offer and its end date, never a made-up one (S17-33).
    offer = (
        ad_templates.clean_offer(data.get("offer")) if row.metadata.get("needs") == "offer" else {}
    )
    return Spec(
        **{
            **common,
            "text": common["text"] or "\n".join(text for text in script.values() if text),
        },
        script=script,
        offer=offer,
        angle=_key(CreativeKind.ANGLE, data.get("angle"), "angle"),
        family=_key(CreativeKind.HOOK_FAMILY, data.get("family"), "family"),
        prompt=ad_templates.clean_prompt(workspace, data.get("prompt")),
        aspect=_vertical(),
        language=language,
        source=source,
        role=ad_templates.role_of(row),
        template=row,
        avatar=avatar,
        extra_avatars=people,
        extra_products=others,
        location=location,
    )


# -----------------------------------------------------------------------------
# Pricing
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Quote:
    lines: list[tuple[str, int]]
    credits: int
    render_s: int
    estimate_id: str
    #: S17: the price in video units; 0 when priced in credits or free. When
    #: set, `credits` is 0 and each line's figure is units.
    units: int = 0
    #: Credits the first frame will cost when the render makes its own.
    keyframe_credits: int = 0

    @property
    def currency(self) -> str:
        return "units" if self.units else "credits"

    def line_rows(self) -> list[dict[str, Any]]:
        key = "units" if self.units else "credits"
        return [
            {"label": label, "credits": 0 if self.units else amount, key: amount}
            for label, amount in self.lines
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": self.line_rows(),
            "credits": self.credits,
            "units": self.units,
            "currency": self.currency,
            "keyframe_credits": self.keyframe_credits,
            "render_s": self.render_s,
            "estimate_id": self.estimate_id,
        }


def _keyframe_credits(spec: Spec) -> int:
    if spec.template is None or spec.source is not None:
        return 0
    if not ad_templates.has_frame(spec.template):
        return 0
    from ai.services.costing import resolve_cost

    return resolve_cost(kind="IMAGE", mode="PRODUCT")


def _digest(*parts: Any) -> str:
    return "est_" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:10]


def quote(spec: Spec, *, free: bool = False, units: bool = False) -> Quote:
    """The priced breakdown. Free lines are left out — "Slow push-in · 0 cr"
    is noise — but every row is still read, so a missing price is an error.

    `free` is a clip on the plan's free allowance: one line, at nothing.
    `units` prices the render in video units (S17): one line, the weight of
    the template's length or of the length row — surcharges are part of it."""
    keyframe = _keyframe_credits(spec)
    if spec.template is not None:
        label = f"{spec.length.label} · {spec.template.label}"
        render_s = int(spec.length.metadata.get("render_s", 0)) + (8 if keyframe else 0)
        if free:
            free_line = [(f"{label} · free", 0)]
            return Quote(
                lines=free_line,
                credits=0,
                render_s=render_s,
                estimate_id=_digest(free_line, 0, keyframe),
                keyframe_credits=keyframe,
            )
        # A template only exists with the S17 flag on, so it is always paid
        # in units when it is not free.
        weight = ad_templates.length_units(spec.template, spec.length.key)
        template_line = [(label, weight)]
        return Quote(
            lines=template_line,
            credits=0,
            units=weight,
            render_s=render_s,
            estimate_id=_digest(template_line, "u", weight, keyframe),
            keyframe_credits=keyframe,
        )
    if units and not free:
        weight = units_of(spec.length)
        unit_line = [(f"{spec.length.label} {spec.mode}", weight)]
        render_s = int(spec.length.metadata.get("render_s", 0)) + (
            len(spec.shots) if spec.mode == VideoMode.REEL else 0
        )
        return Quote(
            lines=unit_line,
            credits=0,
            units=weight,
            render_s=render_s,
            estimate_id=_digest(unit_line, "u", weight),
        )
    if free:
        credits_of(spec.length)
        free_lines = [(f"{spec.length.label} {spec.mode} · free", 0)]
        render_s = int(spec.length.metadata.get("render_s", 0))
        return Quote(
            lines=free_lines, credits=0, render_s=render_s, estimate_id=_digest(free_lines, 0)
        )
    lines: list[tuple[str, int]] = [(f"{spec.length.label} {spec.mode}", credits_of(spec.length))]

    def add(label: str, row: CreativeOption, times: int = 1) -> None:
        amount = credits_of(row) * times
        if amount:
            lines.append((label, amount))

    if spec.mode == VideoMode.CLIP:
        assert spec.motion is not None
        add(spec.motion.label, spec.motion)
        render_s = int(spec.length.metadata.get("render_s", 0))
    else:
        assert spec.style is not None and spec.music is not None
        add(f"{len(spec.shots)} shots", spec.extras["shot"], len(spec.shots))
        add(spec.style.label, spec.style)
        add(f"Music bed · {spec.music.label}", spec.music)
        if spec.captions:
            add(spec.extras["captions"].label, spec.extras["captions"])
        if spec.end_card and "end-card" in spec.extras:
            add(spec.extras["end-card"].label, spec.extras["end-card"])
        render_s = int(spec.length.metadata.get("render_s", 0)) + len(spec.shots)

    credits = sum(amount for _label, amount in lines)
    return Quote(
        lines=lines, credits=credits, render_s=render_s, estimate_id=_digest(lines, credits)
    )


def free_clips_left(workspace: Workspace) -> int:
    """What is left of the plan's free allowance, pooled across the
    organization (L-1) and never reset. A failed render gives its clip back.
    A query, every time (rule 5)."""
    allowance = int(entitlements_for(workspace).feature("free_video_clips") or 0)
    used = (
        VideoRender.objects.filter(workspace__organization_id=workspace.organization_id, free=True)
        .exclude(status=VideoRenderStatus.FAILED)
        .count()
    )
    return max(0, allowance - used)


def require_video(workspace: Workspace, mode: str, *, seconds: int | None = None) -> bool:
    """Entitlement first, availability second — a Free workspace must hear
    "upgrade", not "no vendor configured", which would be true and useless.

    True when the render is on the free allowance: no `video_generation`, a
    clip, no longer than `free_video_max_seconds` (when `seconds` is known),
    and clips left. Anything else on such a plan is the plan's 402."""
    entitlements = entitlements_for(workspace)
    free = False
    if not entitlements.feature("video_generation"):
        allowance = int(entitlements.feature("free_video_clips") or 0)
        longest = int(entitlements.feature("free_video_max_seconds") or 0)
        if (
            mode != VideoMode.CLIP
            or not allowance
            or (seconds is not None and seconds > longest)
            or free_clips_left(workspace) <= 0
        ):
            message = (
                f"Your {allowance} free videos are used. Upgrade to make more."
                if allowance and mode == VideoMode.CLIP and free_clips_left(workspace) <= 0
                else "Your plan does not include video generation."
            )
            raise FeatureNotAvailable(
                message,
                detail={
                    "feature": "video_generation",
                    "plan": entitlements.plan.code,
                    "free_video_clips": allowance,
                    "free_video_max_seconds": longest,
                },
                suggested_plan=suggest_upgrade(entitlements.plan, "video_generation"),
            )
        free = True
    port = get_video_provider() if mode == VideoMode.CLIP else get_video_composer()
    if port is None:
        raise VideoUnavailableError(detail={"mode": mode})
    return free


def paid_in_units(workspace: Workspace) -> bool:
    """S17: with the flag on, video is paid from the video allowance."""
    return ad_templates.is_on(workspace)


def estimate(workspace: Workspace, data: dict[str, Any]) -> Quote:
    require_video(workspace, data["mode"])
    spec = parse(workspace, data)
    return quote(
        spec,
        free=require_video(workspace, spec.mode, seconds=spec.seconds),
        units=paid_in_units(workspace),
    )


def _held(workspace: Workspace, field_name: str) -> int:
    """What renders in flight have been promised, in credits or video units.
    A query, every time (rule 5)."""
    total = VideoRender.objects.filter(workspace=workspace, status__in=ACTIVE).aggregate(
        total=Sum(field_name)
    )["total"]
    return int(total or 0)


def held_credits(workspace: Workspace) -> int:
    return _held(workspace, "credits")


def held_units(workspace: Workspace) -> int:
    return _held(workspace, "units")


# -----------------------------------------------------------------------------
# Starting a render
# -----------------------------------------------------------------------------
def start_render(
    workspace: Workspace,
    *,
    user: User,
    data: dict[str, Any],
    estimate_id: str,
    retry_of: VideoRender | None = None,
    post: Post | None = None,
) -> VideoRender:
    """Hold the confirmed price and queue the render.

    The price is worked out again here and must match the estimate the person
    confirmed: a catalogue edit between the two is a 409 carrying the new
    estimate, never a charge they did not see.
    """
    require_video(workspace, data["mode"])
    spec = parse(workspace, data)
    free = require_video(workspace, spec.mode, seconds=spec.seconds)
    priced = quote(spec, free=free, units=paid_in_units(workspace))
    if priced.estimate_id != estimate_id:
        raise EstimateChangedError(detail={"estimate": priced.as_dict()})
    if retry_of is not None and retry_of.status != VideoRenderStatus.FAILED:
        raise RenderNotReviewableError(
            "Only a failed render can be retried.", detail={"retry_of": retry_of.pk}
        )

    if post is not None:
        # Locked (approved) or gone: the post's own 409s, from the one guard
        # every edit passes — a clip for a post that cannot change is refused
        # before anything is held.
        from content.services.posts import ensure_editable

        ensure_editable(post)

    entitlements = entitlements_for(workspace)
    with transaction.atomic():
        # The same lock every spend takes: two confirmations racing for the
        # last free credits serialise here, and the second sees the first's hold.
        Workspace.objects.select_for_update().filter(pk=workspace.pk).exists()
        if free:
            # The allowance is the organization's: two brands racing for the
            # last free clip serialise on the organization row, and the
            # second hears the 402.
            Organization.objects.select_for_update().filter(pk=workspace.organization_id).exists()
            if free_clips_left(workspace) <= 0:
                require_video(workspace, spec.mode, seconds=spec.seconds)
        if post is not None:
            # **Animating twice never charges twice.** The same clip of the same
            # still for the same post, already rendering or rendered, is that
            # render — a double click, a retried request, a second tab. Checked
            # under the lock, so two racing requests cannot both miss it.
            existing = (
                VideoRender.objects.filter(
                    workspace=workspace,
                    post=post,
                    spec=spec.stored(),
                    status__in=(*ACTIVE, VideoRenderStatus.SUCCEEDED),
                )
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                return existing
        if priced.units:
            entitlements.require_video_units(priced.units, held=held_units(workspace))
        else:
            entitlements.require_credits(priced.credits, held=held_credits(workspace))
        keyframe = None
        if priced.keyframe_credits:
            # The first frame is paid in credits, on pass, by its own
            # generation — preflighted here so a render is never queued
            # toward a frame the balance cannot buy.
            entitlements.require_credits(priced.keyframe_credits, held=held_credits(workspace))
            assert spec.template is not None and spec.product is not None
            keyframe = ad_templates.create_keyframe(
                workspace,
                user=user,
                row=spec.template,
                product=spec.product,
                avatar=spec.avatar,
                location=spec.location,
                extra_avatars=spec.extra_avatars,
                extra_products=spec.extra_products,
            )
        render = VideoRender.objects.create(
            workspace=workspace,
            created_by=user,
            product=spec.product,
            mode=spec.mode,
            spec=spec.stored(),
            text=spec.text,
            source=spec.source or (spec.shots[0].media if spec.shots else None),
            estimate_id=priced.estimate_id,
            credits=priced.credits,
            units=priced.units,
            free=free,
            lines=priced.line_rows(),
            render_s=priced.render_s,
            retry_of=retry_of,
            post=post,
            template=spec.template.key if spec.template else "",
            avatar=spec.avatar,
            keyframe=keyframe,
            labels=disclosure.labels_for(spec.avatar),
            seed=secrets.randbelow(SEED_RANGE),
        )
        if retry_of is not None:
            retry_of.review = VideoReview.RETRIED
            retry_of.save(update_fields=["review", "updated_at"])

    from ai.tasks import run_video_render

    run_video_render.delay(render.pk)
    render.refresh_from_db()  # under eager Celery the render has already run
    return render


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------
def _advance(render: VideoRender, phase: str, progress: float) -> None:
    render.phase = phase
    render.progress = progress
    render.save(update_fields=["phase", "progress", "updated_at"])


def _fail(render: VideoRender, code: str, message: str) -> VideoRender:
    render.status = VideoRenderStatus.FAILED
    render.error_code = code
    render.error = message
    render.finished_at = timezone.now()
    render.save(update_fields=["status", "error_code", "error", "finished_at", "updated_at"])
    return render


def _bytes(asset: MediaAsset) -> bytes:
    with asset.file.open("rb") as handle:
        return bytes(handle.read())


def caption_cues(text: str, *, start: float, end: float) -> list[CaptionCue]:
    """Burned-in captions from the post text (S3-04), timed in code.

    Chunks of `CAPTION_WORDS` words, at most `CAPTION_MAX_CUES` of them, spread
    evenly over `start..end`. Text past the last card is not squeezed in — a
    caption nobody can read in time is worse than a shorter one — and the post
    itself still carries the whole text as its caption.
    """
    words = text.split()
    chunks = [" ".join(words[i : i + CAPTION_WORDS]) for i in range(0, len(words), CAPTION_WORDS)][
        :CAPTION_MAX_CUES
    ]
    if not chunks or end <= start:
        return []
    step = (end - start) / len(chunks)
    return [
        CaptionCue(
            text=chunk, start=round(start + i * step, 2), end=round(start + (i + 1) * step, 2)
        )
        for i, chunk in enumerate(chunks)
    ]


def beat_cues(script: dict[str, str], seconds: float) -> list[CaptionCue]:
    """A template's hook, body and call to action as cues on their beats: the
    hook from the first frame (the ad must read with the sound off), the call
    to action over the close, the body between. An empty part is left out and
    its time goes to its neighbour."""
    edge = min(BEAT_EDGE_SECONDS, seconds / 3)
    windows = {
        "hook": (0.0, edge),
        "body": (edge, seconds - edge),
        "cta": (seconds - edge, seconds),
    }
    parts = [(part, str(script.get(part) or "").strip()) for part in ("hook", "body", "cta")]
    kept = [(part, text) for part, text in parts if text]
    cues: list[CaptionCue] = []
    for index, (part, text) in enumerate(kept):
        start = 0.0 if index == 0 else windows[part][0]
        end = seconds if index == len(kept) - 1 else windows[kept[index + 1][0]][0]
        cues.append(CaptionCue(text=text, start=round(start, 2), end=round(end, 2)))
    return cues


def _logo(workspace: Workspace) -> bytes | None:
    """The workspace's logo file, or `None` when there is none, it cannot be
    read, or it is favicon-sized — a stretched favicon is worse than no logo."""
    if not workspace.logo:
        return None
    try:
        with workspace.logo.open("rb") as handle:
            content = bytes(handle.read())
        with Image.open(io.BytesIO(content)) as picture:
            width, height = picture.size
    except (OSError, ValueError):
        logger.warning("workspace %s: logo unreadable", workspace.pk, exc_info=True)
        return None
    if max(width, height) < LOGO_MIN_SIDE or width * height < LOGO_MIN_AREA:
        return None
    return content


def brand_end_card(
    workspace: Workspace, product: Product | None, *, title: str, call_to_action: str
) -> EndCard:
    """The end card in the brand's own colours (S17-26): the kit's primary as
    the background and its accent for the call-to-action pill, the real logo,
    the product's identity photo. Without a kit, the plain dark card."""
    kit = kit_for(workspace)
    colors = dict(kit.colors) if kit else {}
    background = colors.get("Primary") or END_CARD_BACKGROUND
    accent = colors.get("Accent") or colors.get("Highlight") or ""
    if not accent or accent.upper() == background.upper():
        # One colour only: a pill in the card's own colour would vanish.
        accent = END_CARD_ACCENT if background != END_CARD_ACCENT else END_CARD_BACKGROUND
    photos = identity_photos(product) if product is not None else []
    photo = _bytes(photos[0]) if photos else None
    return EndCard(
        title=title,
        call_to_action=call_to_action,
        seconds=END_CARD_SECONDS,
        background=background,
        accent=accent,
        logo=_logo(workspace),
        # The product cut out of a plain background (S17-31), or its photo.
        image=(graphics.cutout(photo) or photo) if photo is not None else None,
    )


def _end_card(render: VideoRender) -> EndCard:
    title = render.product.name if render.product else render.workspace.name
    cta_key = render.spec.get("cta") or ""
    cta = (
        CreativeOption.objects.filter(kind=CreativeKind.CTA, key=cta_key).first()
        if cta_key
        else None
    )
    short = str((cta.metadata or {}).get("short") or cta.label) if cta else ""
    return brand_end_card(render.workspace, render.product, title=title, call_to_action=short)


def _render_clip(render: VideoRender) -> VideoResult:
    template = (
        CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key=render.template).first()
        if render.template
        else None
    )
    if template is not None and ad_templates.shots_of(template):
        return _render_template_reel(render, template)
    provider = get_video_provider(workspace=render.workspace)
    if provider is None:
        raise VideoUnavailableError
    assert render.source is not None
    motion = (
        None
        if template
        else CreativeOption.objects.filter(
            kind=CreativeKind.MOTION, key=render.spec["motion"]
        ).first()
    )
    aspect = CreativeOption.objects.filter(
        kind=CreativeKind.VIDEO_ASPECT, key=render.spec["aspect"]
    ).first()
    image = _bytes(render.source)
    prompt = (
        ad_templates.motion_prompt(template, seconds=float(render.spec["seconds"]))
        if template
        else (motion.prompt_fragment if motion else "")
    )
    if prompt:
        # The scene, light, camera, cast and mood the person chose, in the same
        # words the still was made with (`creative.look_lines`).
        prompt = _directed(prompt, render)
    if prompt and flag_enabled(render.workspace.organization, STUDIO_REPHRASE):
        # The motion row, rewritten into one brief (`ai.services.rephrase`);
        # the row's own words when the rewrite fails its checks.
        rewritten = rephrase.motion_brief(
            motion=prompt,
            role=render.spec.get("role", "start"),
            seconds=float(render.spec["seconds"]),
            product=render.product.name if render.product else "",
        )
        prompt = rewritten.text
        render.rephrased_by = rewritten.by
        render.rephrased_prompt = "" if rewritten.by == rephrase.TEMPLATE else rewritten.text
        render.save(update_fields=["rephrased_prompt", "rephrased_by"])
    if template is not None:
        # The template's negative, after any rewrite, which may never drop it.
        forbid = ad_templates.place_forbid(
            template, str(render.spec.get("location") or ""), render.workspace
        )
        prompt = " ".join(filter(None, [prompt, ad_templates.avoid_line(template, forbid)]))
    _record_prompt(render, prompt)
    _advance(render, *PHASES[1])
    result: VideoResult = provider.animate(
        image=image,
        role=render.spec.get("role", "start"),
        prompt=prompt,
        aspect=str(aspect.metadata["aspect"]) if aspect else REEL_ASPECT,
        duration_seconds=float(render.spec["seconds"]),
        seed=render.seed,
    )
    if template is not None:
        return _burn_in_beats(render, result)
    return result


def _burn_in_beats(render: VideoRender, clip: VideoResult) -> VideoResult:
    """A single-clip template's hook, body and call to action burned in on
    their beats (S17-26), so the ad reads with the sound off. The clip keeps
    its vendor, model and length; with no composer, or one that fails, the
    clip goes out as it came — words are never worth a paid animation."""
    composer = get_video_composer()
    cues = beat_cues(dict(render.spec.get("script") or {}), float(render.spec["seconds"]))
    if composer is None or not cues:
        return clip
    try:
        burned = composer.caption(clip=clip.content, captions=cues, aspect=REEL_ASPECT)
    except ProviderError:
        logger.warning("render %s: beats not burned in", render.pk, exc_info=True)
        return clip
    return VideoResult(
        content=burned.content,
        mime=burned.mime,
        duration_seconds=burned.duration_seconds,
        provider=clip.provider,
        model=clip.model,
        latency_ms=clip.latency_ms + burned.latency_ms,
    )


def _record_prompt(render: VideoRender, prompt: str) -> None:
    """Saved before the vendor call, so a failed render keeps it too."""
    render.prompt_sent = prompt
    render.save(update_fields=["prompt_sent"])


#: Shots the video model animates; the others are stills, drawn or crisp.
MOVING_USES = frozenset({"frame", "photo", "place"})


def _render_template_reel(render: VideoRender, template: CreativeOption) -> VideoResult:
    """A multi-shot template (T3): one clip per `frame`, `photo` or `place`
    shot, the app screenshot set in as a still, the drawn shots (S17-33: text
    cards, cutouts on a colour field, a photo grid, chat bubbles) made in code,
    a line on each, a brand end card — cut by the composer into one file. One
    render, so one hold and one debit, however many shots; any shot failing
    fails the whole render, unpaid."""
    composer = get_video_composer()
    if composer is None:
        raise VideoUnavailableError
    shots_meta = ad_templates.shots_of(template)
    product = render.product
    assert product is not None
    script = dict(render.spec.get("script") or {})
    language = str(render.spec.get("language") or "en")
    offer = dict(render.spec.get("offer") or {})
    if offer.get("ends"):
        offer["ends_label"] = ad_templates.ends_label(offer["ends"], language)
    tags = product.reference_tags or {}
    photos = identity_photos(product)
    screens = [a for a in photos if tags.get(str(a.pk)) == ad_templates.SCREEN_TAG]
    plain = [a for a in photos if tags.get(str(a.pk)) != ad_templates.SCREEN_TAG] or photos
    places = _own_places(render.workspace) if any(s["use"] == "place" for s in shots_meta) else []
    provider = None
    if any(shot["use"] in MOVING_USES for shot in shots_meta):
        provider = get_video_provider(workspace=render.workspace)
        if provider is None:
            raise VideoUnavailableError
    seconds = float(render.spec["seconds"])
    card = brand_end_card(
        render.workspace,
        product,
        title=product.name,
        call_to_action=script.get("cta", "")[:OVERLAY_MAX],
    )
    per_shot = round((seconds - card.seconds) / len(shots_meta), 2)
    prompt = " ".join(
        filter(
            None,
            [
                _directed(ad_templates.motion_prompt(template), render),
                ad_templates.avoid_line(
                    template,
                    ad_templates.place_forbid(
                        template, str(render.spec.get("location") or ""), render.workspace
                    ),
                ),
            ],
        )
    )
    _record_prompt(render, prompt)
    _advance(render, *PHASES[1])
    shots: list[ReelShot] = []
    counts = {"photo": 0, "screen": 0, "cutout": 0, "place": 0}

    def take(pool: list[MediaAsset], use: str) -> MediaAsset:
        if not pool:
            raise VideoUnavailableError
        picked = pool[counts[use] % len(pool)]
        counts[use] += 1
        return picked

    for shot in shots_meta:
        use = shot["use"]
        line = ad_templates.shot_line(str(shot.get("text") or ""), product, script, offer)
        overlay = line
        if use == "frame":
            assert render.source is not None
            image = _bytes(render.source)
        elif use == "card":
            kicker = ad_templates.shot_line(str(shot.get("kicker") or ""), product, script, offer)
            image = graphics.card(
                line,
                background=card.background,
                kicker=ad_templates.kicker_text(kicker, language),
                strike=bool(shot.get("strike")),
            )
            overlay = ""  # the words are drawn on the card
        elif use == "cutout":
            photo = _bytes(take(plain, "cutout"))
            image = graphics.cutout_field(photo, background=graphics.field_colour(photo))
        elif use == "grid":
            image = graphics.mosaic([_bytes(p) for p in plain], background=card.background) or (
                _bytes(plain[0])
            )
        elif use == "chat":
            image = graphics.chat(
                [(script.get("hook", ""), False), (script.get("body", ""), True)],
                background="#F4F4F2",
                accent=card.accent if card.accent != "#FFFFFF" else card.background,
            )
            overlay = ""
        elif use == "place":
            image = _bytes(take(places, "place"))
        else:
            image = _bytes(take(screens if use == "screen" else plain, use))
        clip = None
        if use in MOVING_USES:
            assert provider is not None
            clip = provider.animate(
                image=image,
                role="start",
                prompt=prompt,
                aspect=REEL_ASPECT,
                # The models' shortest clip; the composer trims it to the shot.
                duration_seconds=float(max(5, -(-per_shot // 1))),
                # One seed for every shot of the render, so they drift less.
                seed=render.seed,
            ).content
        shots.append(
            ReelShot(image=image, seconds=per_shot, overlay=overlay[:OVERLAY_MAX], clip=clip)
        )
    result: VideoResult = composer.compose(
        shots=shots,
        aspect=REEL_ASPECT,
        transition=str(template.metadata.get("transition") or "crossfade"),
        music=None,
        captions=[],
        end_card=card,
        duration_seconds=seconds,
    )
    return result


def _own_places(workspace: Workspace) -> list[MediaAsset]:
    """The brand's own place photos, oldest first (S17-28)."""
    from brand.models import BrandPlace, PlaceSource

    return [
        place.media
        for place in BrandPlace.objects.filter(
            workspace=workspace, source=PlaceSource.UPLOAD, media__isnull=False
        ).select_related("media")
        if place.media is not None
    ]


def _render_reel(render: VideoRender) -> VideoResult:
    composer = get_video_composer()
    if composer is None:
        raise VideoUnavailableError
    spec = render.spec
    seconds = float(spec["seconds"])
    card = _end_card(render) if spec.get("end_card") else None
    body_seconds = seconds - (card.seconds if card else 0.0)
    shots_in = spec["shots"]
    assets = {
        asset.pk: asset
        for asset in MediaAsset.objects.filter(
            workspace=render.workspace, pk__in=[shot["media"] for shot in shots_in]
        )
    }
    per_shot = round(body_seconds / len(shots_in), 2)
    shots = [
        ReelShot(
            image=_bytes(assets[shot["media"]]),
            seconds=per_shot,
            overlay=shot.get("text", "") if spec.get("overlays") else "",
        )
        for shot in shots_in
    ]
    style = CreativeOption.objects.filter(kind=CreativeKind.REEL_STYLE, key=spec["style"]).first()
    _advance(render, *PHASES[1])
    result: VideoResult = composer.compose(
        shots=shots,
        aspect=REEL_ASPECT,
        transition=str((style.metadata or {}).get("transition", "cut")) if style else "cut",
        music=None if spec.get("music") in ("", "none") else spec["music"],
        captions=(
            caption_cues(render.text, start=0.0, end=body_seconds) if spec.get("captions") else []
        ),
        end_card=card,
        duration_seconds=seconds,
    )
    return result


def _persist(render: VideoRender, result: VideoResult) -> MediaAsset:
    upload = SimpleUploadedFile(f"render-{render.pk}.mp4", result.content, content_type=result.mime)
    asset = ingest_media(workspace=render.workspace, upload=upload, source=MediaSource.GENERATED)
    size = (
        CreativeOption.objects.filter(
            kind=CreativeKind.VIDEO_ASPECT, key=render.spec.get("aspect", "")
        )
        .values_list("metadata", flat=True)
        .first()
    )
    width, height = (size or {}).get("size") or (None, None)
    # Set once, on the row this call just made — before anything else can read
    # it — the same way a generated image is linked to its generation.
    asset.derived_from = render.source
    asset.duration_ms = int(result.duration_seconds * 1000)
    asset.width = width
    asset.height = height
    asset.save(update_fields=["derived_from", "duration_ms", "width", "height"])
    return asset


def run_render(render_id: int) -> VideoRender:
    """Idempotent: a render that is not `QUEUED` is returned as it is, so a
    retried task can neither render twice nor charge twice."""
    with transaction.atomic():
        render = VideoRender.objects.select_for_update().get(pk=render_id)
        if render.status != VideoRenderStatus.QUEUED:
            return render
        render.status = VideoRenderStatus.RUNNING
        render.started_at = timezone.now()
        render.phase, render.progress = PHASES[0]
        render.save(update_fields=["status", "started_at", "phase", "progress", "updated_at"])

    if render.source_id is None and render.keyframe_id is not None:
        failed = _make_keyframe(render)
        if failed is not None:
            return failed

    started = time.monotonic()
    try:
        result = _render_clip(render) if render.mode == VideoMode.CLIP else _render_reel(render)
    except VideoUnavailableError:
        return _fail(
            render,
            "video_unavailable",
            "Video rendering is not available right now. No credits were taken.",
        )
    except ProviderError:
        logger.warning("video render %s: provider error", render.pk, exc_info=True)
        return _fail(
            render,
            "provider_error",
            "The video service did not finish the render. No credits were taken.",
        )

    render.provider = result.provider
    render.model = result.model
    render.latency_ms = int((time.monotonic() - started) * 1000)
    _advance(render, *PHASES[2])
    _advance(render, *PHASES[3])
    check = quality.run_video_quality_gate(
        content=result.content,
        mime=result.mime,
        duration_seconds=result.duration_seconds,
        expected_seconds=float(render.spec["seconds"]),
    )
    if not check.passed:
        render.save(update_fields=["provider", "model", "latency_ms", "updated_at"])
        return _fail(
            render, "quality_gate_failed", f"{check.rejected_reason} No credits were taken."
        )

    render.checks = _measure(render, result)
    entitlements = entitlements_for(render.workspace)
    try:
        with transaction.atomic():
            locked = VideoRender.objects.select_for_update().get(pk=render.pk)
            if locked.status != VideoRenderStatus.RUNNING:
                return locked
            if render.units:
                locked.unit_charge = ledger.debit_video_units(
                    render.workspace,
                    render.units,
                    quota=entitlements.quota("included_videos"),
                    note=f"video render {render.pk}",
                )
            else:
                locked.charge = ledger.debit_credits(
                    render.workspace,
                    render.credits,
                    quota=entitlements.quota("monthly_ai_credits"),
                    note=f"video render {render.pk}",
                )
            locked.output = _persist(locked, result)
            locked.status = VideoRenderStatus.SUCCEEDED
            locked.phase = "DONE"
            locked.progress = 1.0
            locked.provider = render.provider
            locked.model = render.model
            locked.latency_ms = render.latency_ms
            locked.checks = render.checks
            locked.finished_at = timezone.now()
            locked.save()
            return locked
    except InsufficientCredits:
        # The balance moved under the hold — a monthly reset, an operator
        # adjustment. The render passed but is not delivered unpaid-for (I2).
        return _fail(
            render,
            "insufficient_credits",
            "There were not enough credits left when the render finished. Nothing was taken.",
        )
    except InsufficientVideoUnits:
        return _fail(
            render,
            "insufficient_video_units",
            "There were not enough videos left when the render finished. Nothing was taken.",
        )


def _measure(render: VideoRender, result: VideoResult) -> list[dict[str, Any]]:
    """The advisory checks on the finished file (S17-35); [] when unmeasurable."""
    from ai.models import QualityGateConfig

    template = (
        CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key=render.template).first()
        if render.template
        else None
    )
    card = (template is not None and bool(ad_templates.shots_of(template))) or (
        render.mode == VideoMode.REEL and bool(render.spec.get("end_card"))
    )
    photos = (
        [_bytes(photo) for photo in identity_photos(render.product)[:3]] if render.product else []
    )
    return render_checks.inspect(
        result.content,
        end_card_seconds=END_CARD_SECONDS if card else 0.0,
        product_photos=photos,
        threshold=QualityGateConfig.get_solo().identity_similarity_threshold,
    )


def _make_keyframe(render: VideoRender) -> VideoRender | None:
    """The template's first frame, made by this render's task: the image
    pipeline's own run (gate, debit on pass), inline because this task is
    already the worker's. `None` when it is ready; the failed render when not."""
    from ai.models import GenerationStatus
    from ai.services.pipeline import run_generation

    generation = render.keyframe
    assert generation is not None
    try:
        generation = run_generation(generation, n=1)
    except ProviderError:
        logger.warning("render %s: keyframe provider error", render.pk, exc_info=True)
        # Not left PENDING: nothing will run it again.
        generation.status = GenerationStatus.FAILED
        generation.error_detail = {"reason": "provider_error"}
        generation.save(update_fields=["status", "error_detail", "updated_at"])
    variant = (
        generation.variants.filter(media_asset__isnull=False).order_by("id").first()
        if generation.status == GenerationStatus.SUCCEEDED
        else None
    )
    if variant is None:
        return _fail(
            render,
            "keyframe_failed",
            "The first frame could not be made. No videos or credits were taken.",
        )
    render.source = variant.media_asset
    render.spec = {**render.spec, "source": variant.media_asset_id}
    render.save(update_fields=["source", "spec", "updated_at"])
    return None


# -----------------------------------------------------------------------------
# After the render
# -----------------------------------------------------------------------------
_REVIEWS: dict[str, tuple[str, ...]] = {
    VideoReview.ACCEPTED: (VideoRenderStatus.SUCCEEDED,),
    VideoReview.DISCARDED: (VideoRenderStatus.SUCCEEDED,),
    VideoReview.HIDDEN: (VideoRenderStatus.SUCCEEDED, VideoRenderStatus.FAILED),
}


def review(render: VideoRender, value: str) -> VideoRender:
    """Accept, discard, hide — or `""` to undo. A render already on a post is
    final; one still rendering has nothing to review yet (409 for both)."""
    if render.post_id is not None:
        raise RenderNotReviewableError(
            "This render is already on a post.", detail={"post": render.post_id}
        )
    if value and render.status not in _REVIEWS[value]:
        raise RenderNotReviewableError(detail={"status": render.status, "review": value})
    if not value and render.review == VideoReview.RETRIED:
        raise RenderNotReviewableError("A retried render stays retried.")
    render.review = value
    render.save(update_fields=["review", "updated_at"])
    return render


def send(
    workspace: Workspace, *, user: User, renders: list[VideoRender], platforms: list[str]
) -> list[Post]:
    """Accepted renders → draft posts, one each (S3-06).

    Each post is made by `create_post`, the clip as its media and the render's
    text as its caption, marked as the product's own work — exactly what the
    image Studio's commit does. Scheduling and approval are the post's path;
    nothing here reaches a platform (L-2).
    """
    for render in renders:
        if render.review != VideoReview.ACCEPTED or render.output_id is None:
            raise RenderNotReviewableError(
                "Only accepted, finished renders can be sent.", detail={"render": render.pk}
            )
        if render.post_id is not None:
            raise RenderNotReviewableError(
                "This render is already on a post.", detail={"render": render.pk}
            )
    posts: list[Post] = []
    with transaction.atomic():
        for render in renders:
            assert render.output is not None
            post = create_post(
                workspace=workspace,
                author=user,
                # A person on screen is labelled through to publishing; the
                # pre-publish checks refuse a caption that loses the label.
                master_body=disclosure.with_disclosure(
                    render.text, render.labels, language=render.spec.get("language", "")
                ),
                category=workspace.category,
                media_assets=[render.output],
            )
            update_post(
                post,
                reason="studio",
                product=render.product,
                source=PostSource.AI,
                planned_platforms=list(dict.fromkeys(platforms)),
            )
            render.post = post
            render.save(update_fields=["post", "updated_at"])
            posts.append(post)
    return posts


def queue_positions(workspace: Workspace) -> dict[int, int]:
    """1-based place in line for each queued render of this workspace."""
    queued = (
        VideoRender.objects.filter(workspace=workspace, status=VideoRenderStatus.QUEUED)
        .order_by("created_at", "id")
        .values_list("pk", flat=True)
    )
    return {pk: index + 1 for index, pk in enumerate(queued)}
