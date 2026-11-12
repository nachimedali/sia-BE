"""Pictures drawn in code, never by a model (S17-31, S17-33).

Everything here is deterministic and free: the same input is the same picture.
A model asked for a product's outline, a card's words or a chat bubble invents
letterforms and edges; Pillow draws exactly what it is given.
"""

from __future__ import annotations

import io
from typing import Any, cast

from PIL import Image, ImageChops, ImageDraw, ImageFont

#: The work is bounded: a cutout is for an end card a few hundred pixels wide.
CUTOUT_MAX_SIDE = 1200
#: How far a pixel's colour may sit from the background's and still be it.
BACKGROUND_TOLERANCE = 28
#: The share of the border that must be that one colour for the photo to count
#: as shot on a plain background. Below it the photo keeps its background.
PLAIN_BORDER = 0.92
#: A "product" smaller than this share of the picture is noise, not a product.
MIN_PRODUCT_SHARE = 0.02


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def cutout(content: bytes) -> bytes | None:
    """The product cut out of a plain background, as a cropped transparent PNG,
    or `None` when the photo is not on a plain background (or unreadable).

    shortcut: a flood fill from the border, so it handles studio and catalogue
    photos only; a photo on a busy background keeps its background. Upgrade to
    a segmentation model when lifestyle photos must be cut out too."""
    try:
        with Image.open(io.BytesIO(content)) as opened:
            image = opened.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    image.thumbnail((CUTOUT_MAX_SIDE, CUTOUT_MAX_SIDE))
    width, height = image.size
    if width < 16 or height < 16:
        return None
    pixels = image.load()
    assert pixels is not None
    border = [
        *((x, 0) for x in range(width)),
        *((x, height - 1) for x in range(width)),
        *((0, y) for y in range(height)),
        *((width - 1, y) for y in range(height)),
    ]
    colours = [cast(tuple[int, int, int], pixels[x, y]) for x, y in border]
    background = tuple(sorted(c[i] for c in colours)[len(colours) // 2] for i in range(3))
    plain = sum(
        1
        for c in colours
        if max(abs(c[i] - background[i]) for i in range(3)) <= BACKGROUND_TOLERANCE
    )
    if plain / len(colours) < PLAIN_BORDER:
        return None

    # 0 where the picture is the background's colour, 255 elsewhere; then the
    # background-coloured area touching the border becomes 128 — a hole in the
    # product that happens to share the colour stays part of the product.
    distance = ImageChops.difference(image, Image.new("RGB", image.size, background)).convert("L")
    regions = distance.point(lambda v: 0 if v <= BACKGROUND_TOLERANCE else 255)
    for seed in border:
        if regions.getpixel(seed) == 0:
            ImageDraw.floodfill(regions, seed, 128, thresh=0)
    alpha = regions.point(lambda v: 0 if v == 128 else 255)
    box = alpha.getbbox()
    if box is None:
        return None
    kept = (box[2] - box[0]) * (box[3] - box[1])
    if kept < MIN_PRODUCT_SHARE * width * height:
        return None
    result = image.convert("RGBA")
    result.putalpha(alpha)
    return _png(result.crop(box))


# -----------------------------------------------------------------------------
# Graphic shots (S17-33): drawn at the reel's size, inside the safe area
# -----------------------------------------------------------------------------
FRAME = (1080, 1920)
#: Clear of the platforms' own buttons (goose-skills `review-finished-ad`):
#: the top 11 %, the bottom 21 % and the right 13 % of a 9:16 frame.
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 0.10, 0.13, 0.12, 0.22


def _rgb(colour: str) -> tuple[int, int, int]:
    value = (colour or "").strip().lstrip("#")
    if len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
        value = "111111"
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _ink(background: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = (channel / 255 for channel in background)
    return (17, 17, 17) if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.55 else (255, 255, 255)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from django.conf import settings

    path = str(getattr(settings, "VIDEO_FONT_FILE", "") or "")
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _wrap(text: str, font: Any, width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if not current or font.getlength(trial) <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return [line for line in lines if line] or [""]


def _fit_text(text: str, width: float, height: float, start: int) -> tuple[Any, list[str]]:
    """The largest font from `start` down whose wrapped lines fit the box."""
    size = start
    while True:
        font = _font(size)
        lines = _wrap(text, font, width)
        if len(lines) * size * 1.25 <= height or size <= 28:
            return font, lines
        size = int(size * 0.88)


def _safe_box(size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    return (
        int(width * SAFE_LEFT),
        int(height * SAFE_TOP),
        int(width * (1 - SAFE_RIGHT)),
        int(height * (1 - SAFE_BOTTOM)),
    )


def card(
    text: str,
    *,
    background: str,
    kicker: str = "",
    strike: bool = False,
    size: tuple[int, int] = FRAME,
) -> bytes:
    """A text card: the words, large and centred in the safe area on the
    brand's colour, with a small kicker above (`FACT`, `Until 30 Nov`) and,
    for a myth, a line struck through them."""
    field = _rgb(background)
    ink = _ink(field)
    image = Image.new("RGB", size, field)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = _safe_box(size)
    font, lines = _fit_text(text, right - left, (bottom - top) * 0.7, start=size[0] // 9)
    line_height = int(font.size * 1.25) if hasattr(font, "size") else 40
    block = line_height * len(lines)
    y = top + ((bottom - top) - block) // 2
    if kicker:
        small_size = max(24, size[0] // 26)
        draw.text(
            (left, y - int(small_size * 1.8)), kicker.upper(), font=_font(small_size), fill=ink
        )
    for line in lines:
        draw.text((left, y), line, font=font, fill=ink)
        if strike and line:
            middle = y + line_height // 2
            draw.line(
                (left, middle, left + int(font.getlength(line)), middle),
                fill=ink,
                width=max(4, line_height // 10),
            )
        y += line_height
    return _png(image)


def _paste_centred(
    image: Image.Image, picture: Image.Image, box: tuple[int, int, int, int]
) -> None:
    left, top, right, bottom = box
    picture.thumbnail((right - left, bottom - top))
    x = left + ((right - left) - picture.width) // 2
    y = top + ((bottom - top) - picture.height) // 2
    image.paste(picture, (x, y), picture if picture.mode == "RGBA" else None)


def cutout_field(photo: bytes, *, background: str, size: tuple[int, int] = FRAME) -> bytes:
    """The product on a colour field: cut out of a plain background where it
    can be, else its photo, centred in the safe area."""
    image = Image.new("RGB", size, _rgb(background))
    content = cutout(photo) or photo
    try:
        with Image.open(io.BytesIO(content)) as opened:
            picture = opened.convert("RGBA")
    except (OSError, ValueError):
        return _png(image)
    left, top, right, bottom = _safe_box(size)
    _paste_centred(image, picture, (left, top, right, bottom))
    return _png(image)


def mosaic(photos: list[bytes], *, background: str, size: tuple[int, int] = FRAME) -> bytes | None:
    """The product's photos as one grid (2x2 up to 3x3) in the safe area, or
    `None` with fewer than two."""
    pictures: list[Image.Image] = []
    for content in photos[:9]:
        try:
            with Image.open(io.BytesIO(content)) as opened:
                pictures.append(opened.convert("RGB"))
        except (OSError, ValueError):
            continue
    if len(pictures) < 2:
        return None
    columns = 2 if len(pictures) <= 4 else 3
    rows = -(-len(pictures) // columns)
    image = Image.new("RGB", size, _rgb(background))
    left, top, right, bottom = _safe_box(size)
    gap = size[0] // 60
    cell = min(
        (right - left - gap * (columns - 1)) // columns, (bottom - top - gap * (rows - 1)) // rows
    )
    grid_left = left + ((right - left) - (cell * columns + gap * (columns - 1))) // 2
    grid_top = top + ((bottom - top) - (cell * rows + gap * (rows - 1))) // 2
    for index, picture in enumerate(pictures):
        side = min(picture.size)
        square = picture.crop(
            (
                (picture.width - side) // 2,
                (picture.height - side) // 2,
                (picture.width + side) // 2,
                (picture.height + side) // 2,
            )
        ).resize((cell, cell))
        row, column = divmod(index, columns)
        image.paste(square, (grid_left + column * (cell + gap), grid_top + row * (cell + gap)))
    return _png(image)


def chat(
    messages: list[tuple[str, bool]],
    *,
    background: str,
    accent: str,
    size: tuple[int, int] = FRAME,
) -> bytes:
    """A conversation as plain bubbles — theirs on the left, the brand's on the
    right in its colour. No phone, no app chrome, no notification: a drawn
    chat must never pass for a real screen (goose-skills `ad-policy-check`)."""
    field = _rgb(background)
    mine = _rgb(accent)
    theirs = tuple(
        min(255, int(c * 0.9 + 25)) if sum(field) < 382 else int(c * 0.92) for c in field
    )
    image = Image.new("RGB", size, field)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = _safe_box(size)
    font = _font(size[0] // 22)
    pad = size[0] // 40
    width = int((right - left) * 0.78)
    y = top + (bottom - top) // 6
    for text, outgoing in messages:
        lines = _wrap(text, font, width - 2 * pad)
        line_height = int(font.size * 1.3) if hasattr(font, "size") else 36
        bubble_w = int(max(font.getlength(line) for line in lines)) + 2 * pad
        bubble_h = line_height * len(lines) + 2 * pad
        x = right - bubble_w if outgoing else left
        fill = mine if outgoing else theirs
        draw.rounded_rectangle((x, y, x + bubble_w, y + bubble_h), radius=pad * 2, fill=fill)
        ink = _ink(cast(tuple[int, int, int], fill))
        for index, line in enumerate(lines):
            draw.text((x + pad, y + pad + index * line_height), line, font=font, fill=ink)
        y += bubble_h + pad * 2
        if y > bottom:
            break
    return _png(image)


def field_colour(photo: bytes) -> str:
    """A light field matched to a product photo (a variant's own colour, soft):
    its average colour, carried most of the way to white."""
    try:
        with Image.open(io.BytesIO(photo)) as opened:
            small = opened.convert("RGB").resize((16, 16))
    except (OSError, ValueError):
        return "#F4F4F2"
    pixels = cast(list[tuple[int, int, int]], list(small.get_flattened_data()))
    average = [sum(p[i] for p in pixels) // len(pixels) for i in range(3)]
    light = [int(c + (255 - c) * 0.72) for c in average]
    return "#{:02X}{:02X}{:02X}".format(*light)
