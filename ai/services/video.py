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
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
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
from ai.services import quality
from ai.services.creative import VIDEO_KINDS
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import InsufficientCredits, OCCSError, ProviderError, StateConflict
from content.models import MediaAsset, MediaKind, MediaSource, Post, PostSource
from content.services.media import ingest_media
from content.services.posts import create_post, update_post
from products.models import Product
from workspaces.models import Workspace

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
        if self.mode == VideoMode.CLIP:
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
    common: dict[str, Any] = {"mode": mode, "length": length, "text": text, "product": product}

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

    aspect = CreativeOption.objects.filter(
        kind=CreativeKind.VIDEO_ASPECT, is_active=True, metadata__aspect=REEL_ASPECT
    ).first()
    if aspect is None:
        raise VideoPriceNotConfiguredError(
            "The 9:16 frame is missing from the video catalogue.",
            detail={"kind": CreativeKind.VIDEO_ASPECT, "aspect": REEL_ASPECT},
        )
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


# -----------------------------------------------------------------------------
# Pricing
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Quote:
    lines: list[tuple[str, int]]
    credits: int
    render_s: int
    estimate_id: str

    def line_rows(self) -> list[dict[str, Any]]:
        return [{"label": label, "credits": amount} for label, amount in self.lines]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": self.line_rows(),
            "credits": self.credits,
            "render_s": self.render_s,
            "estimate_id": self.estimate_id,
        }


def quote(spec: Spec) -> Quote:
    """The priced breakdown. Free lines are left out — "Slow push-in · 0 cr"
    is noise — but every row is still read, so a missing price is an error."""
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
    digest = hashlib.sha256(json.dumps([lines, credits]).encode()).hexdigest()[:10]
    return Quote(lines=lines, credits=credits, render_s=render_s, estimate_id=f"est_{digest}")


def require_video(workspace: Workspace, mode: str) -> None:
    """Entitlement first, availability second — a Free workspace must hear
    "upgrade", not "no vendor configured", which would be true and useless."""
    entitlements_for(workspace).require_feature("video_generation")
    port = get_video_provider() if mode == VideoMode.CLIP else get_video_composer()
    if port is None:
        raise VideoUnavailableError(detail={"mode": mode})


def estimate(workspace: Workspace, data: dict[str, Any]) -> Quote:
    require_video(workspace, data["mode"])
    return quote(parse(workspace, data))


def held_credits(workspace: Workspace) -> int:
    """Credits promised to renders in flight. A query, every time (rule 5)."""
    total = VideoRender.objects.filter(workspace=workspace, status__in=ACTIVE).aggregate(
        total=Sum("credits")
    )["total"]
    return int(total or 0)


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
    priced = quote(spec)
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
        entitlements.require_credits(priced.credits, held=held_credits(workspace))
        render = VideoRender.objects.create(
            workspace=workspace,
            created_by=user,
            product=spec.product,
            mode=spec.mode,
            spec=spec.stored(),
            text=spec.text,
            source=spec.source or spec.shots[0].media,
            estimate_id=priced.estimate_id,
            credits=priced.credits,
            lines=priced.line_rows(),
            render_s=priced.render_s,
            retry_of=retry_of,
            post=post,
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


def _end_card(render: VideoRender) -> EndCard:
    title = render.product.name if render.product else render.workspace.name
    cta_key = render.spec.get("cta") or ""
    cta = (
        CreativeOption.objects.filter(kind=CreativeKind.CTA, key=cta_key).first()
        if cta_key
        else None
    )
    short = str((cta.metadata or {}).get("short") or cta.label) if cta else ""
    return EndCard(title=title, call_to_action=short, seconds=END_CARD_SECONDS)


def _render_clip(render: VideoRender) -> VideoResult:
    provider = get_video_provider()
    if provider is None:
        raise VideoUnavailableError
    assert render.source is not None
    motion = CreativeOption.objects.filter(
        kind=CreativeKind.MOTION, key=render.spec["motion"]
    ).first()
    aspect = CreativeOption.objects.filter(
        kind=CreativeKind.VIDEO_ASPECT, key=render.spec["aspect"]
    ).first()
    image = _bytes(render.source)
    _advance(render, *PHASES[1])
    result: VideoResult = provider.animate(
        image=image,
        role=render.spec.get("role", "start"),
        prompt=motion.prompt_fragment if motion else "",
        aspect=str(aspect.metadata["aspect"]) if aspect else REEL_ASPECT,
        duration_seconds=float(render.spec["seconds"]),
    )
    return result


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

    entitlements = entitlements_for(render.workspace)
    try:
        with transaction.atomic():
            locked = VideoRender.objects.select_for_update().get(pk=render.pk)
            if locked.status != VideoRenderStatus.RUNNING:
                return locked
            entry = ledger.debit_credits(
                render.workspace,
                render.credits,
                quota=entitlements.quota("monthly_ai_credits"),
                note=f"video render {render.pk}",
            )
            locked.output = _persist(locked, result)
            locked.charge = entry
            locked.status = VideoRenderStatus.SUCCEEDED
            locked.phase = "DONE"
            locked.progress = 1.0
            locked.provider = render.provider
            locked.model = render.model
            locked.latency_ms = render.latency_ms
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
                master_body=render.text,
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
