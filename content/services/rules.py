"""Platform rules — data, not conditionals (design.md §8.6).

Character limits, media caps and hashtag placement are the facts that differ
per platform. Keeping them as one row per platform means the Adaptation Engine
reads a table instead of branching on platform name, and retuning a limit is an
edit to this file rather than a new `if` somewhere in the pipeline.

These are practical public limits, not billing quotas — I8 (design.md §3)
governs commercial numbers that must be admin-editable `Plan`/`GenerationCost`
rows; a platform's caption limit is an external fact about that platform, not a
price OCCS charges, so a constants module is the right home for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from content.models import MediaKind, Platform


@dataclass(frozen=True)
class PlatformRule:
    char_limit: int
    max_media: int
    allowed_media_kinds: frozenset[str]
    hashtag_placement: str  # "inline" | "trailing_block"
    supports_thread: bool


PLATFORM_RULES: dict[str, PlatformRule] = {
    Platform.INSTAGRAM: PlatformRule(
        char_limit=2200,
        max_media=10,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="trailing_block",
        supports_thread=False,
    ),
    Platform.LINKEDIN: PlatformRule(
        char_limit=3000,
        max_media=9,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
    ),
    Platform.TIKTOK: PlatformRule(
        char_limit=2200,
        max_media=1,
        allowed_media_kinds=frozenset({MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
    ),
    Platform.YOUTUBE: PlatformRule(
        char_limit=5000,
        max_media=1,
        allowed_media_kinds=frozenset({MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
    ),
    Platform.THREADS: PlatformRule(
        char_limit=500,
        max_media=10,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=True,
    ),
    Platform.FACEBOOK: PlatformRule(
        char_limit=5000,
        max_media=10,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
    ),
}
