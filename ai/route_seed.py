"""The model each task starts on, per package (pricing.plan.md, Phase 4).

Data, not behaviour: `ai/migrations/0021_seed_model_routes` creates these with
`get_or_create` and never overwrites a row an operator has edited in admin
(AI → Model routes). Choices and their reasons are in `pricing.audit.md` §3 to §5.

Model ids are each vendor's exact id as listed on 2026-10-19/21: NVIDIA's
hosted free list, Google's Gemini API price page, OpenRouter's
`/api/v1/videos/models`. The providers are the labels this deployment's ports
answer to (`LLM_PROVIDER`, `IMAGE_PROVIDER`, `VIDEO_PROVIDER`); a route for a
provider the deployment does not point at is skipped, never sent.
"""

from __future__ import annotations

from typing import Any

TEXT = "nvidia"
IMAGE = "nanobanana"
VIDEO = "openrouter"

#: What a seeded row sets besides its `(task, plan)` key — read by the
#: migration and by `seed_model_routes`, so the two cannot disagree.
FIELDS = ("provider", "model", "fallback_model", "params", "note")

GLIMMER = "meta/muse-glimmer-30b"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"
TEXT_FALLBACK = "deepseek-ai/deepseek-v4.1-flash"
VISION_FALLBACK = "meta/llama-3.2-11b-vision-instruct"
BULK_FALLBACK = "google/gemma-4-31b-it"

NANO_BANANA_21 = "gemini-nano-banana-2.1"
NANO_BANANA_2 = "gemini-3.1-flash-image"

GROK_LITE = "x-ai/grok-imagine-video-1.5-lite"
WAN_3 = "alibaba/wan-3.0"
KLING_3_STD = "kwaivgi/kling-v3.0-std"


def _route(
    task: str,
    provider: str,
    model: str,
    *,
    plan: str | None = None,
    fallback: str = "",
    params: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "task": task,
        "plan": plan,
        "provider": provider,
        "model": model,
        "fallback_model": fallback,
        "params": params or {},
        "note": note,
    }


ROUTES: list[dict[str, Any]] = [
    # --- text: NVIDIA hosted API (audit §3) --------------------------------
    *(
        _route(task, TEXT, GLIMMER, fallback=TEXT_FALLBACK, note="Writing: vision-capable 30B.")
        for task in (
            "studio_text",
            "image_copy",
            "brand_import",
            "campaign_plan",
            "digest",
            "tools",
            "ad_script",
        )
    ),
    _route(
        "caption_from_image",
        TEXT,
        GLIMMER,
        fallback=VISION_FALLBACK,
        note="Vision: the image is attached, so the fallback must see images too.",
    ),
    _route(
        "constraint_check",
        TEXT,
        GLIMMER,
        fallback=VISION_FALLBACK,
        note="Vision: one call per image variant.",
    ),
    *(
        _route(
            task,
            TEXT,
            LIGHTNING,
            fallback=BULK_FALLBACK,
            note="Bulk: ~3B active parameters, light on the shared 40/min budget.",
        )
        for task in ("sentiment", "inbox_coding", "rephrase")
    ),
    # --- images: Google direct (audit §4) -----------------------------------
    _route(
        "image_studio",
        IMAGE,
        NANO_BANANA_21,
        fallback=NANO_BANANA_2,
        params={"image_size": "2K"},
        note="Paid packages render at 2K.",
    ),
    _route(
        "image_studio",
        IMAGE,
        NANO_BANANA_21,
        plan="spark",
        fallback=NANO_BANANA_2,
        params={"image_size": "1K"},
        note="Spark drafts at 1K.",
    ),
    _route(
        "image_autopilot",
        IMAGE,
        NANO_BANANA_21,
        fallback=NANO_BANANA_2,
        params={"image_size": "1K", "batch": False},
        note=(
            "batch is off until batch results are collected by a beat job: the "
            "adapter polls for about a minute and the vendor allows 24 hours."
        ),
    ),
    # --- video: OpenRouter (audit §5) ----------------------------------------
    _route(
        "video_start",
        VIDEO,
        GROK_LITE,
        fallback=WAN_3,
        params={"resolution": "1080p"},
        note="First-frame clips and text-to-video.",
    ),
    *(
        _route(
            "video_start",
            VIDEO,
            GROK_LITE,
            plan=plan,
            fallback=WAN_3,
            params={"resolution": "720p"},
            note=f"{plan.title()} renders at 720p.",
        )
        for plan in ("discovery", "essential")
    ),
    _route(
        "video_end",
        VIDEO,
        WAN_3,
        fallback=KLING_3_STD,
        params={"resolution": "1080p", "audio": True},
        note="Grok takes a first frame only; Wan 3.0 lands on a last frame and has audio.",
    ),
    *(
        _route(
            "video_end",
            VIDEO,
            WAN_3,
            plan=plan,
            fallback=KLING_3_STD,
            params={"resolution": "720p", "audio": True},
            note=f"{plan.title()} renders at 720p.",
        )
        for plan in ("discovery", "essential")
    ),
    # S17: avatar portraits and their reference angles. 1K is enough for a
    # face the image model is then given as a reference.
    _route(
        "avatar_portrait",
        IMAGE,
        NANO_BANANA_21,
        fallback=NANO_BANANA_2,
        params={"image_size": "1K"},
        note="Avatar portraits and angles, 1K.",
    ),
    # Spark's four free clips: Wan only, 720p, and no fallback — a free render
    # that fails is given back, never retried on a dearer model.
    *(
        _route(
            task,
            VIDEO,
            WAN_3,
            plan="spark",
            params={"resolution": "720p", "audio": False},
            note="Spark's free clips: Wan 3.0 at 720p, no fallback.",
        )
        for task in ("video_start", "video_end")
    ),
]
