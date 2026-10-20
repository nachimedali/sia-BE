"""The brand kit, read — one reader for everything that draws (S1-06).

The Studio's image prompt, the pre-publish palette check and, later, the
graphics renderer all want the same three facts: the brand's colours, its fonts
and whether it has a logo. They read them here, from the active `BrandCore`, so
there is one answer and one place that decides what counts as a brand colour.
Nothing here writes: the kit changes only by applying an import (a new version).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from brand.models import BrandCore
from workspaces.models import Workspace

#: The roles that name a brand colour. A "Colour" row is a spare swatch the
#: reading found, not something the brand chose, so it is not held to.
BRAND_ROLES = ("Primary", "Accent", "Highlight")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


@dataclass(frozen=True)
class BrandKit:
    version: int
    colors: tuple[tuple[str, str], ...]  # (role, "#RRGGBB"), most important first
    fonts: dict[str, str]  # slot -> family
    has_logo: bool

    @property
    def hexes(self) -> list[str]:
        return [hex_ for _role, hex_ in self.colors]


def kit_for(workspace: Workspace) -> BrandKit | None:
    """The workspace's kit, or `None` when nothing has been applied (or the
    applied import found no brand colour — an empty kit is no kit)."""
    core = BrandCore.objects.filter(workspace=workspace).order_by("-version").first()
    if core is None:
        return None
    palette: dict[str, Any] = ((core.sections or {}).get("palette") or {}).get("value") or {}
    colors = tuple(
        (str(row["role"]), str(row["hex"]).upper())
        for row in palette.get("colors") or []
        if isinstance(row, dict)
        and row.get("role") in BRAND_ROLES
        and _HEX.fullmatch(str(row.get("hex") or ""))
    )
    fonts = {
        slot: str(spec["name"])
        for slot, spec in (palette.get("fonts") or {}).items()
        if isinstance(spec, dict) and spec.get("name")
    }
    if not colors and not fonts:
        return None
    return BrandKit(core.version, colors, fonts, bool(workspace.logo))


def prompt_lines(kit: BrandKit | None) -> list[str]:
    """What the image model is told about the kit. Colours only: a model asked
    to render a font name invents letterforms, so fonts are for the graphics
    renderer, not for a photograph."""
    if kit is None or not kit.colors:
        return []
    named = ", ".join(f"{role.lower()} {hex_}" for role, hex_ in kit.colors)
    return [
        f"Brand colours: {named}. Let these colours lead in the styling, props and graphics where "
        "the scene allows, unless the brief names a palette. Never recolour the product itself."
    ]
