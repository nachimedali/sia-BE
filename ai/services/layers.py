"""S21-02 — layers: the Studio's 2D pass over a finished clip or still.

Each layer is drawn here, in code, from the person's words and the brand's
colours, and composited over the render by the composer (`VideoComposer.overlay`)
or pasted onto the still. Nothing here reaches a model: the words stay sharp,
and editing them costs nothing.

Drawn bubbles carry no app chrome, no app name and no icon — a made-up chat
must never pass for a real screen (the rule `graphics.chat` follows).

Positions are percentages of the frame (`x`, `y` the top-left corner, `w` the
width); a layer's height follows its words. Times are seconds from the start;
`end` null means to the end of the clip. Stills ignore the times.
"""

from __future__ import annotations

import io
import logging
from dataclasses import asdict, dataclass
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from PIL import Image, ImageDraw, ImageOps

from ai.services import video
from ai.services.graphics import _font, _ink, _png, _rgb, _wrap
from common.exceptions import OCCSError, ProviderError
from content.models import MediaSource
from content.services.editing import _read, _store
from content.services.media import ingest_media

logger = logging.getLogger(__name__)

KINDS = frozenset({"message", "notification", "comment", "tag", "logo", "cta", "caption"})
CAPTION_STYLES = ("bold", "boxed", "minimal")
SIDES = ("left", "right")
MAX_LAYERS = 24
TEXT_MAX = 200
NAME_MAX = 40
MIN_WIDTH = 10

#: Frame sizes the layers are drawn for, by aspect.
FRAMES: dict[str, tuple[int, int]] = {"9:16": (1080, 1920), "4:5": (1080, 1350)}


class LayersInvalidError(OCCSError):
    """400: a layer cannot be drawn as asked. `detail` names the layer's index
    and the field to fix (`layers` for the list itself)."""

    default_code = "layers_invalid"
    default_detail = "One of the layers cannot be drawn as it is."


@dataclass(frozen=True)
class Layer:
    kind: str
    text: str
    x: float
    y: float
    w: float
    start: float
    end: float | None
    name: str = ""
    side: str = "left"
    style: str = "bold"


@dataclass(frozen=True)
class Kit:
    """The brand's colours for this project (S21: edited per project)."""

    primary: str = "#111111"
    accent: str = "#FFFFFF"
    logo: bool = False

    @classmethod
    def of(cls, value: Any) -> Kit:
        value = value if isinstance(value, dict) else {}
        return cls(
            primary=_hex(value.get("primary"), "#111111"),
            accent=_hex(value.get("accent"), "#FFFFFF"),
            logo=bool(value.get("logo")),
        )


@dataclass(frozen=True)
class Drawn:
    image: Image.Image
    x: int
    y: int


def _hex(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    body = text.lstrip("#")
    if len(body) == 6 and all(c in "0123456789abcdefABCDEF" for c in body):
        return f"#{body.upper()}"
    return fallback


def _bad(index: int, field: str) -> LayersInvalidError:
    return LayersInvalidError(detail={"index": index, "field": field})


def _number(raw: dict[str, Any], key: str, index: int, low: float, high: float) -> float:
    try:
        value = float(raw.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _bad(index, key) from None
    if not low <= value <= high:
        raise _bad(index, key)
    return value


def clean(items: Any, *, seconds: float | None) -> list[Layer]:
    """The layers as asked, checked. `seconds` is the clip's length (`None`
    for a still, where times do not apply)."""
    if not isinstance(items, list) or len(items) > MAX_LAYERS:
        raise LayersInvalidError(detail={"index": -1, "field": "layers"})
    out: list[Layer] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise _bad(index, "kind")
        kind = str(raw.get("kind") or "")
        if kind not in KINDS:
            raise _bad(index, "kind")
        text = str(raw.get("text") or "").strip()
        if (not text and kind != "logo") or len(text) > TEXT_MAX:
            raise _bad(index, "text")
        name = str(raw.get("name") or "").strip()
        if len(name) > NAME_MAX:
            raise _bad(index, "name")
        side = str(raw.get("side") or "left")
        if side not in SIDES:
            raise _bad(index, "side")
        style = str(raw.get("style") or "bold")
        if style not in CAPTION_STYLES:
            raise _bad(index, "style")
        x = _number(raw, "x", index, 0, 100)
        y = _number(raw, "y", index, 0, 100)
        w = _number(raw, "w", index, MIN_WIDTH, 100)
        start = _number(raw, "start", index, 0, seconds if seconds is not None else 3600)
        if seconds is not None and seconds > 0 and start >= seconds:
            raise _bad(index, "start")
        end: float | None = None
        if raw.get("end") is not None:
            end = _number(raw, "end", index, 0, 3600)
            if end <= start:
                raise _bad(index, "end")
        out.append(Layer(kind, text, x, y, w, start, end, name, side, style))
    return out


# -----------------------------------------------------------------------------
# Drawing
# -----------------------------------------------------------------------------
_WHITE = (255, 255, 255, 255)
_INK = (17, 17, 17, 255)
_GREY = (110, 110, 106, 255)


def _rgba(colour: str) -> tuple[int, int, int, int]:
    return (*_rgb(colour), 255)


def _ink_on(colour: str) -> tuple[int, int, int, int]:
    return (*_ink(_rgb(colour)), 255)


def _card(
    lines: list[str],
    font: Any,
    *,
    width: int,
    pad: int,
    fill: tuple[int, int, int, int],
    ink: tuple[int, int, int, int],
    radius: int,
    head: str = "",
    head_font: Any = None,
    head_ink: tuple[int, int, int, int] = _GREY,
    fit: bool = True,
) -> Image.Image:
    line_height = int(getattr(font, "size", 32) * 1.3)
    head_height = int(getattr(head_font, "size", 24) * 1.4) if head else 0
    longest = max((int(font.getlength(line)) for line in lines), default=0)
    box_w = min(width, longest + 2 * pad)
    box_h = head_height + line_height * len(lines) + 2 * pad
    if head and head_font is not None:
        box_w = min(width, max(box_w, int(head_font.getlength(head)) + 2 * pad))
    if not fit:
        box_w = width
    image = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=radius, fill=fill)
    if head:
        draw.text((pad, pad), head, font=head_font, fill=head_ink)
    for index, line in enumerate(lines):
        draw.text((pad, pad + head_height + index * line_height), line, font=font, fill=ink)
    return image


def _logo_image(logo: bytes | None, width: int) -> Image.Image | None:
    if not logo:
        return None
    try:
        with Image.open(io.BytesIO(logo)) as opened:
            picture = opened.convert("RGBA")
    except (OSError, ValueError):
        return None
    height = max(1, round(picture.height * width / max(picture.width, 1)))
    return picture.resize((width, height))


def draw(
    layer: Layer, *, frame: tuple[int, int], kit: Kit, logo: bytes | None, brand: str
) -> Drawn:
    """One layer as a transparent picture and where it sits on the frame."""
    frame_w, frame_h = frame
    width = max(1, round(frame_w * layer.w / 100))
    base = frame_w // 26
    pad = frame_w // 45
    radius = pad * 2
    font = _font(base)
    small = _font(int(base * 0.72))

    def wrap(size_font: Any) -> list[str]:
        return _wrap(layer.text, size_font, width - 2 * pad)

    image: Image.Image
    if layer.kind == "message":
        mine = layer.side == "right"
        fill = _rgba(kit.accent) if mine else _WHITE
        image = _card(
            wrap(font),
            font,
            width=width,
            pad=pad,
            fill=fill,
            ink=_ink_on(kit.accent) if mine else _INK,
            radius=radius,
            head=layer.name,
            head_font=small,
        )
    elif layer.kind == "notification":
        image = _card(
            wrap(font),
            font,
            width=width,
            pad=pad,
            fill=_WHITE,
            ink=_INK,
            radius=radius,
            head=layer.name or brand,
            head_font=small,
            head_ink=_INK,
            fit=False,
        )
    elif layer.kind == "comment":
        head = f"@{layer.name}" if layer.name else ""
        image = _card(
            wrap(font),
            font,
            width=width,
            pad=pad,
            fill=_WHITE,
            ink=_INK,
            radius=pad,
            head=head,
            head_font=small,
        )
    elif layer.kind == "tag":
        image = _card(
            wrap(small),
            small,
            width=width,
            pad=pad,
            fill=_rgba(kit.accent),
            ink=_ink_on(kit.accent),
            radius=radius * 2,
        )
    elif layer.kind == "cta":
        big = _font(int(base * 1.25))
        image = _card(
            wrap(big),
            big,
            width=width,
            pad=pad * 2,
            fill=_rgba(kit.primary),
            ink=_ink_on(kit.primary),
            radius=radius,
            fit=False,
        )
    elif layer.kind == "logo":
        picture = _logo_image(logo, width) if kit.logo else None
        if picture is None:
            text = layer.text or brand
            image = _card(
                _wrap(text, small, width - 2 * pad),
                small,
                width=width,
                pad=pad,
                fill=_rgba(kit.primary),
                ink=_ink_on(kit.primary),
                radius=radius,
            )
        else:
            image = picture
    else:  # caption
        big = _font(int(base * 1.1))
        lines = wrap(big)
        line_height = int(getattr(big, "size", 40) * 1.3)
        box_h = line_height * len(lines) + 2 * pad
        image = Image.new("RGBA", (width, box_h), (0, 0, 0, 0))
        pen = ImageDraw.Draw(image)
        if layer.style == "boxed":
            pen.rounded_rectangle((0, 0, width - 1, box_h - 1), radius=pad, fill=(0, 0, 0, 200))
        for index, line in enumerate(lines):
            line_w = int(big.getlength(line))
            at = ((width - line_w) // 2, pad + index * line_height)
            if layer.style == "bold":
                pen.text(
                    at,
                    line,
                    font=big,
                    fill=_WHITE,
                    stroke_width=max(2, base // 10),
                    stroke_fill=(0, 0, 0, 255),
                )
            elif layer.style == "minimal":
                pen.text((at[0] + 2, at[1] + 2), line, font=big, fill=(0, 0, 0, 140))
                pen.text(at, line, font=big, fill=_WHITE)
            else:
                pen.text(at, line, font=big, fill=_WHITE)

    x = round(frame_w * layer.x / 100)
    y = round(frame_h * layer.y / 100)
    # Kept inside the frame: a layer is never cut by the edge of the picture.
    if image.width > frame_w - x:
        image = image.resize(
            (max(1, frame_w - x), max(1, round(image.height * (frame_w - x) / image.width)))
        )
    y = max(0, min(y, frame_h - image.height))
    return Drawn(image=image, x=x, y=y)


def crop_to(content: bytes, aspect: str) -> Image.Image:
    """The still centre-cropped to the aspect and scaled to its frame."""
    with Image.open(io.BytesIO(content)) as opened:
        return ImageOps.fit(opened.convert("RGB"), FRAMES[aspect])


def compose_still(
    content: bytes, *, aspect: str, items: list[Layer], kit: Kit, logo: bytes | None, brand: str
) -> bytes:
    """The still at its aspect with every layer pasted on, as a JPEG."""
    picture = crop_to(content, aspect).convert("RGBA")
    for item in items:
        drawn = draw(item, frame=picture.size, kit=kit, logo=logo, brand=brand)
        picture.alpha_composite(drawn.image.convert("RGBA"), (drawn.x, drawn.y))
    out = io.BytesIO()
    picture.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()


# -----------------------------------------------------------------------------
# Applying them
# -----------------------------------------------------------------------------
def _brand(workspace: Any, items: list[Layer], kit: Kit) -> tuple[bytes | None, str]:
    """The logo file only when a logo layer will draw it: reading it costs a
    storage read on every pass."""
    wanted = kit.logo and any(item.kind == "logo" for item in items)
    return (video._logo(workspace) if wanted else None), str(getattr(workspace, "name", "") or "")


def request_video_layers(render: Any, items: Any, kit: Any) -> Any:
    """Checks the pass and queues it (free: no hold, no debit)."""
    from ai.models import VideoRenderStatus
    from ai.tasks import run_video_layers as task

    if render.post_id is not None:
        raise video.RenderSentError(detail={"render": render.pk})
    if (
        render.status != VideoRenderStatus.SUCCEEDED
        or (render.plain_id or render.output_id) is None
    ):
        raise video.RenderNotReadyError(detail={"render": render.pk})
    if render.layers_status == "running":
        raise video.LayersRunningError(detail={"render": render.pk})
    base = render.plain or render.output
    seconds = (base.duration_ms or 0) / 1000 or float(render.spec.get("seconds") or 0) or None
    checked = clean(items, seconds=seconds)
    render.layers = [asdict(layer) for layer in checked]
    render.spec = {**render.spec, "kit": asdict(Kit.of(kit))}
    render.layers_status = "running"
    render.save(update_fields=["layers", "spec", "layers_status", "updated_at"])
    task.delay(render.pk)
    return render


def _as_layer(raw: dict[str, Any]) -> Layer:
    return Layer(**{key: raw[key] for key in Layer.__dataclass_fields__ if key in raw})


def run_video_layers(render_id: int) -> Any:
    """Idempotent: a render not `running` its layers is left as it is."""
    from ai.models import VideoRender
    from ai.providers.base import OverlayLayer
    from ai.providers.video import get_video_composer

    render = VideoRender.objects.select_related("plain", "output", "workspace").get(pk=render_id)
    if render.layers_status != "running":
        return render
    base = render.plain or render.output
    try:
        items = [_as_layer(raw) for raw in render.layers]
        if not items:
            output = base
        else:
            composer = get_video_composer()
            if composer is None or base is None:
                raise ProviderError("No composer on this machine.")
            kit = Kit.of(render.spec.get("kit"))
            logo, brand = _brand(render.workspace, items, kit)
            frame = FRAMES[video.REEL_ASPECT]
            drawn = [draw(item, frame=frame, kit=kit, logo=logo, brand=brand) for item in items]
            with base.file.open("rb") as handle:
                clip = handle.read()
            result = composer.overlay(
                clip=clip,
                layers=[
                    OverlayLayer(image=_png(d.image), x=d.x, y=d.y, start=i.start, end=i.end)
                    for d, i in zip(drawn, items, strict=True)
                ],
                aspect=video.REEL_ASPECT,
            )
            upload = SimpleUploadedFile(
                f"render-{render.pk}-layers.mp4", result.content, content_type=result.mime
            )
            output = ingest_media(
                workspace=render.workspace, upload=upload, source=MediaSource.GENERATED
            )
            output.derived_from = base
            output.duration_ms = int(result.duration_seconds * 1000) or base.duration_ms
            output.width, output.height = frame
            output.save(update_fields=["derived_from", "duration_ms", "width", "height"])
    except (ProviderError, OSError, ValueError):
        logger.warning("render %s: layers not drawn", render.pk, exc_info=True)
        VideoRender.objects.filter(pk=render.pk).update(layers_status="failed")
        render.layers_status = "failed"
        return render
    with transaction.atomic():
        locked = VideoRender.objects.select_for_update().get(pk=render.pk)
        if locked.post_id is not None:  # sent meanwhile: its media stays as sent
            locked.layers_status = ""
            locked.save(update_fields=["layers_status", "updated_at"])
            return locked
        locked.plain = base
        locked.output = output
        locked.layers_status = ""
        locked.save(update_fields=["plain", "output", "layers_status", "updated_at"])
    return locked


def layer_still(*, workspace: Any, media: Any, aspect: str, items: Any, kit: Any) -> Any:
    """A new still: `media` at `aspect` with the layers on it, derived from it."""
    if aspect not in FRAMES:
        raise LayersInvalidError(detail={"index": -1, "field": "aspect"})
    checked = clean(items, seconds=None)
    colours = Kit.of(kit)
    logo, brand = _brand(workspace, checked, colours)
    try:
        picture = compose_still(
            _read(media), aspect=aspect, items=checked, kit=colours, logo=logo, brand=brand
        )
    except (OSError, ValueError):
        raise LayersInvalidError(detail={"index": -1, "field": "media"}) from None
    return _store(
        media, content=picture, mime="image/jpeg", filename=f"still-{media.pk}-layers.jpg"
    )


def send_still(
    *, workspace: Any, user: Any, media: Any, text: str, language: str, platforms: list[str]
) -> Any:
    """A finished still → one draft post, the way `variants.commit` sends the
    Studio's images: marked as the Studio's work, with the product and the
    generation behind it, and the AI-generated label when a drawn person is
    in the picture (`disclosure.shows_drawn_person` follows `derived_from`).
    Approval is the post's own path; nothing here publishes (L-2)."""
    from ai.services import disclosure
    from content.models import PostSource
    from content.services.posts import create_post, update_post

    origin, seen = media, 0
    while origin is not None and origin.generation_id is None and seen < 4:
        origin, seen = origin.derived_from, seen + 1
    generation = origin.generation if origin is not None and origin.generation_id else None
    labels = [disclosure.AI_GENERATED] if disclosure.shows_drawn_person(media) else []
    post = create_post(
        workspace=workspace,
        author=user,
        master_body=disclosure.with_disclosure(text, labels, language=language),
        category=generation.category if generation else None,
        media_assets=[media],
    )
    update_post(
        post,
        reason="studio",
        product=generation.product if generation else None,
        generation=generation,
        source=PostSource.AI,
        planned_platforms=list(dict.fromkeys(platforms)),
    )
    return post
