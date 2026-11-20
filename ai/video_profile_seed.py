"""What each video model accepts and how it is best told (S19-01).

Data, not behaviour: `ai/migrations/0050_seed_video_profiles` creates these
with `get_or_create` on the slug, so a profile edited in admin (AI → Video
model profiles) is never overwritten.

The facts — lengths, last frame, seed, audio — are OpenRouter's
`GET /api/v1/videos/models` as read on 2026-11-15 (`upgrade_rendering.md` §4).
The word budgets and timecode syntax come from the prompting guides cited in
§6.2. No guide says Grok follows timecodes; until the parked timing test runs,
every profile sends seconds (`0-2 s:`), which a model that ignores them still
reads as actions in order.
"""

from __future__ import annotations

from typing import Any

FIELDS = ("label", "budget_words", "timecodes", "durations", "last_frame", "seed", "audio", "note")


def _span(low: int, high: int) -> list[int]:
    return list(range(low, high + 1))


def _profile(
    slug: str,
    label: str,
    *,
    budget: int,
    durations: list[int],
    last_frame: bool,
    seed: bool,
    audio: bool,
    timecodes: str = "seconds",
    note: str = "",
) -> dict[str, Any]:
    return {
        "slug": slug,
        "label": label,
        "budget_words": budget,
        "timecodes": timecodes,
        "durations": durations,
        "last_frame": last_frame,
        "seed": seed,
        "audio": audio,
        "note": note,
    }


PROFILES: list[dict[str, Any]] = [
    _profile(
        "x-ai/grok-imagine-video-1.5-lite",
        "Grok Imagine 1.5 Lite",
        budget=120,
        durations=_span(1, 15),
        last_frame=False,
        seed=False,
        # OpenRouter lists `generate_audio: true` (2026-11-19); lip sync passed (S17-12).
        audio=True,
        note="No published budget; its guide lists 'multiple scene changes' as a mistake.",
    ),
    _profile(
        "x-ai/grok-imagine-video-1.5",
        "Grok Imagine 1.5",
        budget=120,
        durations=_span(1, 15),
        last_frame=False,
        seed=False,
        audio=False,
    ),
    _profile(
        "kwaivgi/kling-v3.0-std",
        "Kling 3.0 standard",
        budget=100,
        durations=_span(3, 15),
        last_frame=True,
        seed=False,
        audio=True,
        note="'A compact directing note'. Audio costs 50% more.",
    ),
    _profile(
        "kwaivgi/kling-v3.0-pro",
        "Kling 3.0 pro",
        budget=100,
        durations=_span(3, 15),
        last_frame=True,
        seed=False,
        audio=True,
        note="Listed at 720p by OpenRouter, read as 1080p from Kling's price table.",
    ),
    _profile(
        "google/gemini-omni-1.2-flash",
        "Gemini Omni 1.2 Flash",
        budget=200,
        durations=_span(3, 15),
        last_frame=True,
        seed=True,
        audio=True,
        note="Budget from the README examples (100-200 words); audio included in the price.",
    ),
    *(
        _profile(
            slug,
            label,
            budget=250,
            durations=[4, 6, 8],
            last_frame=True,
            seed=True,
            audio=True,
            note="4, 6 or 8 s a call: a 10 s clip is two calls.",
        )
        for slug, label in (
            ("google/veo-3.1-fast", "Veo 3.1 Fast"),
            ("google/veo-3.1-lite", "Veo 3.1 Lite"),
            ("google/veo-3.1", "Veo 3.1"),
        )
    ),
    _profile(
        "bytedance/seedance-2.0",
        "Seedance 2.0",
        budget=150,
        durations=_span(4, 15),
        last_frame=True,
        seed=True,
        audio=True,
        timecodes="brackets",
        note="80-150 words for an insert, up to 250 for multi-shot.",
    ),
    _profile(
        "alibaba/wan-3.0",
        "Wan 3.0",
        budget=200,
        durations=_span(2, 30),
        last_frame=True,
        seed=True,
        audio=True,
        note="No guide; OpenMontage's Wan 2.2 row says 200-400 words.",
    ),
]
