"""Finding the workspace's logo in a picture (steps-plan S4-01).

Plain template matching with Pillow, no vendor: the logo is searched for at a
few sizes and proportions on a small copy of the picture, comparing contrast
-normalised greyscale over the logo's own opaque pixels. That is deliberately a
modest detector — it finds a logo that is placed on the picture, not one that
is rotated, heavily recoloured or half hidden — so its three answers are worded
for what they can honestly know:

* `found`      the logo is there, at this place, at about the right proportions;
* `distorted`  something logo-shaped is there, stretched against its own shape;
* `not_found`  nothing matched — which is "we could not find it", not proof it
               is absent. The caller words it that way.

Its reach is stated, not hidden: the logo has to be at least roughly a seventh
of the picture's width and stand clear of what is behind it. A smaller or
camouflaged one is reported as not found, and the message says why that is not
proof it is missing. A logo too plain to recognise (a flat block of one colour) cannot be searched
for at all, and says so rather than matching anything.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageOps, ImageStat

#: The picture is searched at this width; small enough that the search is a
#: fraction of a second, large enough that a logo ~8% of the width is ~8 px.
SEARCH_WIDTH = 80
#: Logo widths tried, as a fraction of the picture's width.
SCALES = tuple(round(0.08 * 1.13**n, 4) for n in range(11))  # 8% .. ~28%
#: Width-to-height proportions tried against the logo's own, after the logo's
#: own proportions (1.0) found nothing: a match only at one of these is a logo
#: that has been stretched.
STRETCHES = (0.5, 0.67, 0.8, 1.25, 1.5, 2.0)
#: The stretched pass uses every other scale; it only has to be roughly right.
STRETCH_SCALES = SCALES[::2]
#: Mean contrast-normalised grey difference (0-255) under which a window counts
#: as the logo. Lower is stricter.
MATCH_BELOW = 42.0
#: Under this contrast (standard deviation of the logo's greys) a logo has no
#: shape to look for.
MIN_LOGO_CONTRAST = 14.0
STEP = 2


@dataclass(frozen=True)
class LogoMatch:
    state: str  # found | distorted | not_found | unsearchable
    x: float = 0.0  # left edge, 0-1 of the picture's width
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    score: float = 0.0

    @property
    def corner(self) -> str:
        """Where it sits, in words a person would use."""
        if self.state not in ("found", "distorted"):
            return ""
        cx, cy = self.x + self.width / 2, self.y + self.height / 2
        horizontal = "left" if cx < 0.4 else "right" if cx > 0.6 else "centre"
        vertical = "top" if cy < 0.4 else "bottom" if cy > 0.6 else "middle"
        if horizontal == "centre" and vertical == "middle":
            return "centre"
        return f"{vertical} {horizontal}".replace("middle ", "").replace(" centre", "")


def _logo_layers(logo_bytes: bytes) -> tuple[Image.Image, Image.Image]:
    """The logo as greyscale plus a mask of the pixels that are really logo."""
    with Image.open(io.BytesIO(logo_bytes)) as source:
        rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=alpha)
    mask = alpha.point(lambda value: 255 if value > 128 else 0)
    if ImageStat.Stat(mask).mean[0] < 8:  # all but invisible
        mask = Image.new("L", rgba.size, 255)
    return flat.convert("L"), mask


#: A window whose logo-pixels and surrounding pixels differ by less than this
#: (0-255, raw greys) has no logo-like contrast in it, however it matches after
#: stretching: a flat patch of sky stretched to full range looks like anything.
MIN_WINDOW_CONTRAST = 20.0


@dataclass
class _Template:
    """The logo at one size and proportion, with what the scorer needs."""

    stretch: float
    width: int
    height: int
    grey: Image.Image
    inside: Image.Image
    outside: Image.Image | None
    contrast: float


def _template(
    grey_logo: Image.Image, mask: Image.Image, width: int, stretch: float
) -> _Template | None:
    height = max(6, round(width / (grey_logo.width / grey_logo.height * stretch)))
    grey = ImageOps.autocontrast(grey_logo.resize((width, height)))
    inside = mask.resize((width, height))
    if ImageStat.Stat(inside).mean[0] < 8:
        return None
    outside = ImageChops.invert(inside)
    # With no background around the logo there is nothing to contrast against,
    # and the match rests on the shape alone.
    if ImageStat.Stat(outside).mean[0] < 8:
        return _Template(stretch, width, height, grey, inside, None, 0.0)
    contrast = ImageStat.Stat(grey, outside).mean[0] - ImageStat.Stat(grey, inside).mean[0]
    return _Template(stretch, width, height, grey, inside, outside, contrast)


def _score(picture: Image.Image, left: int, top: int, t: _Template) -> float | None:
    """How far the window is from the logo (0 identical .. 255), or `None` when
    the window has no logo-like contrast to speak of."""
    raw = picture.crop((left, top, left + t.width, top + t.height))
    if t.outside is not None:
        contrast = ImageStat.Stat(raw, t.outside).mean[0] - ImageStat.Stat(raw, t.inside).mean[0]
        if contrast * t.contrast <= 0 or abs(contrast) < MIN_WINDOW_CONTRAST:
            return None
    elif ImageStat.Stat(raw).stddev[0] < MIN_WINDOW_CONTRAST:
        return None
    diff = ImageChops.difference(ImageOps.autocontrast(raw), t.grey)
    return float(ImageStat.Stat(diff, t.inside).mean[0])


#: Coarse candidates kept for the finer look.
KEEP = 8


def _search(
    picture: Image.Image,
    grey_logo: Image.Image,
    mask: Image.Image,
    stretches: tuple[float, ...],
    scales: tuple[float, ...],
) -> tuple[float, _Template, int, int] | None:
    """The best window at any of these sizes and proportions. A coarse pass
    strides over the picture; the few best spots are then re-scored at every
    pixel around them, because a stride would otherwise miss a small logo by
    half its own width."""
    full_w, full_h = picture.size
    coarse: list[tuple[float, _Template, int, int]] = []
    for scale in scales:
        for stretch in stretches:
            t = _template(grey_logo, mask, max(6, round(SEARCH_WIDTH * scale)), stretch)
            if t is None or t.width >= full_w or t.height >= full_h:
                continue
            for top in range(0, full_h - t.height + 1, STEP):
                for left in range(0, full_w - t.width + 1, STEP):
                    score = _score(picture, left, top, t)
                    if score is not None:
                        coarse.append((score, t, left, top))
                        if len(coarse) > KEEP * 40:
                            coarse.sort(key=lambda row: row[0])
                            del coarse[KEEP:]
    coarse.sort(key=lambda row: row[0])
    best: tuple[float, _Template, int, int] | None = None
    for _coarse_score, coarse_t, left, top in coarse[:KEEP]:
        # A size either side as well: sizes are tried on a grid, and the logo
        # is rarely exactly on it.
        for dw in (-2, -1, 0, 1, 2):
            t = _template(grey_logo, mask, coarse_t.width + dw, coarse_t.stretch)
            if t is None:
                continue
            for dy in range(-STEP, STEP + 1):
                for dx in range(-STEP, STEP + 1):
                    x, y = left + dx, top + dy
                    if x < 0 or y < 0 or x + t.width > full_w or y + t.height > full_h:
                        continue
                    score = _score(picture, x, y, t)
                    if score is not None and (best is None or score < best[0]):
                        best = (score, t, x, y)
    return best


def find_logo(image_bytes: bytes, logo_bytes: bytes) -> LogoMatch:
    grey_logo, mask = _logo_layers(logo_bytes)
    if ImageStat.Stat(grey_logo).stddev[0] < MIN_LOGO_CONTRAST:
        return LogoMatch("unsearchable")

    with Image.open(io.BytesIO(image_bytes)) as source:
        picture = source.convert("L")
    ratio = SEARCH_WIDTH / picture.width
    picture = picture.resize((SEARCH_WIDTH, max(1, round(picture.height * ratio))))
    full_w, full_h = picture.size

    def match(state: str, best: tuple[float, _Template, int, int]) -> LogoMatch:
        score, t, left, top = best
        return LogoMatch(
            state, left / full_w, top / full_h, t.width / full_w, t.height / full_h, score / 255
        )

    best = _search(picture, grey_logo, mask, (1.0,), SCALES)
    if best is not None and best[0] <= MATCH_BELOW:
        return match("found", best)
    # Only now try other proportions: a logo that fits at its own shape is not
    # distorted, however well a stretched one would also have scored.
    stretched = _search(picture, grey_logo, mask, STRETCHES, STRETCH_SCALES)
    if stretched is not None and stretched[0] <= MATCH_BELOW:
        return match("distorted", stretched)
    return LogoMatch("not_found", score=0.0 if best is None else best[0] / 255)
