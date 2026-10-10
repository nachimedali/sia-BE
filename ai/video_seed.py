"""The catalog behind the Studio's Motion step (steps-plan S3).

The same contract as `ai/creative_seed.py`: data, not behaviour, and every row
an operator can retune in admin. **Every price here is a row too** — a length,
a motion, a style, a music bed and the per-shot and caption surcharges are
`metadata.credits`, read by `ai.services.video.estimate` and nowhere else
(Part 7 rule 10). `ai/migrations/0012_video_catalog` seeds it with
`get_or_create`, so an edited row is never overwritten.

* `video_length` — what is being made and for how long. `metadata.mode` is
  `clip` (one still, animated) or `reel` (several shots, cut together);
  `render_s` is the honest wait a render of it takes; a reel row carries the
  shot range it accepts.
* `motion` — how a clip moves. The fragment is camera direction for the video
  model; `metadata.preview` names the Studio's CSS approximation of it.
* `video_aspect` — the frame. `metadata.size` is the master's pixel size.
* `reel_style` — how a reel's shots are joined.
* `music` — the licensed bed under a reel, or none.
* `video_extra` — surcharges that are not a choice: one row per shot, one for
  burned-in captions.

Motion fragments follow the image catalog's rule that the product is never
redesigned: they say how the camera moves, never what the product looks like.
"""

from __future__ import annotations

from typing import Any

from ai.creative_seed import SEED_VERSION

_PLAY = ["M8 5v14l11-7z"]
_CLOCK = ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 7v5l3 2"]


def _row(
    key: str,
    label: str,
    *,
    description: str = "",
    icon: list[str] | None = None,
    fragment: str = "",
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "description": description,
        "icon_paths": icon or [],
        "colors": [],
        "prompt_fragment": fragment,
        "metadata": {**metadata, "seed": SEED_VERSION},
    }


LENGTHS = [
    _row(
        "clip-5",
        "5 s",
        description="Loops well",
        icon=_CLOCK,
        mode="clip",
        seconds=5,
        credits=6,
        render_s=12,
        default=True,
    ),
    _row(
        "clip-10",
        "10 s",
        description="Room to breathe",
        icon=_CLOCK,
        mode="clip",
        seconds=10,
        credits=10,
        render_s=18,
    ),
    _row(
        "reel-15",
        "15 s",
        description="Stories, Reels",
        icon=_PLAY,
        mode="reel",
        seconds=15,
        credits=12,
        render_s=20,
        min_shots=3,
        max_shots=5,
        default=True,
    ),
    _row(
        "reel-30",
        "30 s",
        description="Full story",
        icon=_PLAY,
        mode="reel",
        seconds=30,
        credits=20,
        render_s=26,
        min_shots=3,
        max_shots=5,
    ),
]

MOTIONS = [
    _row(
        "push",
        "Slow push-in",
        description="The camera glides toward the product",
        icon=["M4 12h12", "M12 8l4 4-4 4", "M20 5v14"],
        fragment=(
            "a slow, steady dolly push-in toward the product, ending a little closer than it "
            "began, with no change to the product itself"
        ),
        credits=0,
        preview="push",
        default=True,
    ),
    _row(
        "orbit",
        "Half orbit",
        description="Turns gently around the product",
        icon=["M4 12a8 4 0 1 0 16 0a8 4 0 1 0-16 0", "M12 8v8"],
        fragment=(
            "a gentle half orbit around the product at a constant distance, keeping it centred "
            "and its label facing the camera at the start and the end"
        ),
        credits=2,
        preview="orbit",
    ),
    _row(
        "parallax",
        "Parallax drift",
        description="The background slides, the product holds",
        icon=["M3 7h18", "M3 12h18", "M3 17h18"],
        fragment=(
            "a slow lateral parallax drift: the background slides gently while the product "
            "holds its place and scale"
        ),
        credits=0,
        preview="parallax",
    ),
    _row(
        "light-sweep",
        "Light sweep",
        description="A warm beam crosses the frame",
        icon=["M4 20L20 4", "M9 20L20 9", "M4 15L15 4"],
        fragment=(
            "a locked-off camera while a soft warm beam of light sweeps once across the frame "
            "and the product"
        ),
        credits=1,
        preview="reveal",
    ),
    _row(
        "handheld",
        "Handheld",
        description="A small, natural sway",
        icon=["M5 12c2-3 4 3 6 0s4 3 6 0 2 0 2 0"],
        fragment=(
            "a subtle handheld sway, as if filmed by a person standing still, the product "
            "staying sharp and in frame"
        ),
        credits=0,
        preview="handheld",
    ),
]

ASPECTS = [
    _row("9-16", "9:16", description="Reels", aspect="9:16", size=[1080, 1920], default=True),
    _row("4-5", "4:5", description="Feed", aspect="4:5", size=[1080, 1350]),
    _row("1-1", "1:1", description="Square", aspect="1:1", size=[1080, 1080]),
]

REEL_STYLES = [
    _row(
        "cuts",
        "Quick cuts",
        description="One shot per beat, no fades",
        fragment="hard cuts on the beat, one shot each, no transitions",
        credits=0,
        transition="cut",
    ),
    _row(
        "glide",
        "Story glide",
        description="Slow zooms and soft cross-fades",
        fragment="a slow zoom on every shot and soft cross-fades between them",
        credits=0,
        transition="crossfade",
        default=True,
    ),
    _row(
        "reveal",
        "Hero reveal",
        description="Builds up to the last shot",
        fragment="builds from detail shots to a full hero reveal of the product on the last shot",
        credits=1,
        transition="build",
    ),
]

MUSIC = [
    _row(
        "oud",
        "Oud lounge",
        description="Warm · 92 BPM · licensed",
        credits=1,
        bpm=92,
        licensed=True,
        default=True,
    ),
    _row(
        "mezoued",
        "Mezoued pop",
        description="Upbeat · 118 BPM · licensed",
        credits=1,
        bpm=118,
        licensed=True,
    ),
    _row(
        "acoustic",
        "Acoustic morning",
        description="Light guitar · 84 BPM · licensed",
        credits=1,
        bpm=84,
        licensed=True,
    ),
    _row("none", "No music", description="Captions only, for sound-off", credits=0),
]

#: Surcharges that are not a choice. `shot` is per shot in a reel; `captions`
#: is once per reel with burned-in captions on. `end-card` is free today and a
#: row so pricing it is an admin edit, not a deploy.
EXTRAS = [
    _row("shot", "Shot", description="Per shot in a reel", credits=1),
    _row("captions", "Burned-in captions", description="From the post text", credits=1),
    _row("end-card", "End card", description="Product name and call to action", credits=0),
]

VIDEO_CATALOG: dict[str, list[dict[str, Any]]] = {
    "video_length": LENGTHS,
    "motion": MOTIONS,
    "video_aspect": ASPECTS,
    "reel_style": REEL_STYLES,
    "music": MUSIC,
    "video_extra": EXTRAS,
}
