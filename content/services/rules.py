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
from typing import Any

from content.models import MediaKind, Platform

# --- shared option definitions ----------------------------------------------
#
# Declared once and referenced by the platforms that offer them, because
# "first comment" means the same thing on Instagram and LinkedIn and two copies
# would eventually disagree about its length limit.

FIRST_COMMENT = ("first_comment", "The first comment, posted immediately after")
LOCATION = ("location_id", "Place / location tag")


#: The current `platform_options` shape. Stored on every `PostTarget` so a
#: payload rendered under an older shape can be recognised and reprocessed
#: rather than silently misread — the same reasoning as `MetricSnapshot.
#: schema_version`. Bump it when an option changes meaning, not when one is
#: added: a new key an old row simply lacks needs no version.
OPTIONS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Option:
    """One per-platform composer field, declared rather than coded (P1-05).

    **This declaration is the whole of Phase 4's cost.** If adding X/Twitter,
    Pinterest and Google Business Profile means adding rows here, Phase 1's
    abstraction held. If it means new conditionals in the adaptation engine,
    P4-06's hard stop applies and the abstraction is what needs repairing —
    which is exactly why this exists before those platforms do.

    `kind` is deliberately a tiny vocabulary. Anything richer would be a schema
    language, and a schema language in a dataclass is a schema language nobody
    can validate.
    """

    key: str
    kind: str  # "str" | "int" | "bool" | "url" | "choice" | "list[str]"
    label: str
    #: `choice` only. Ignored otherwise rather than raising, so a row is not
    #: forced to carry a field its kind has no use for.
    choices: tuple[str, ...] = ()
    max_length: int | None = None
    #: What an absent key means. Never `None` for a `bool` — a tri-state
    #: boolean is how "unset" and "false" start being confused.
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class PlatformRule:
    char_limit: int
    max_media: int
    allowed_media_kinds: frozenset[str]
    hashtag_placement: str  # "inline" | "trailing_block"
    supports_thread: bool
    #: Per-platform composer fields (P1-11). Empty is the honest default: a
    #: platform with nothing to configure declares nothing, rather than
    #: inheriting a union of every other platform's options.
    options: tuple[Option, ...] = ()


PLATFORM_RULES: dict[str, PlatformRule] = {
    Platform.INSTAGRAM: PlatformRule(
        char_limit=2200,
        max_media=10,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="trailing_block",
        supports_thread=False,
        options=(
            Option(FIRST_COMMENT[0], "str", FIRST_COMMENT[1], max_length=2200),
            Option(LOCATION[0], "str", LOCATION[1], max_length=64),
            Option("collab_handles", "list[str]", "Invite as collaborators"),
            Option("share_to_feed", "bool", "Also share a reel to the feed", default=True),
        ),
    ),
    Platform.LINKEDIN: PlatformRule(
        char_limit=3000,
        max_media=9,
        allowed_media_kinds=frozenset({MediaKind.IMAGE, MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option(FIRST_COMMENT[0], "str", FIRST_COMMENT[1], max_length=3000),
            Option(
                "visibility",
                "choice",
                "Who can see this",
                choices=("PUBLIC", "CONNECTIONS"),
                default="PUBLIC",
            ),
        ),
    ),
    Platform.TIKTOK: PlatformRule(
        char_limit=2200,
        max_media=1,
        allowed_media_kinds=frozenset({MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option("allow_comments", "bool", "Allow comments", default=True),
            Option("allow_duet", "bool", "Allow duet", default=True),
        ),
    ),
    Platform.YOUTUBE: PlatformRule(
        char_limit=5000,
        max_media=1,
        allowed_media_kinds=frozenset({MediaKind.VIDEO}),
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option("title", "str", "Video title", max_length=100, required=True),
            Option("thumbnail_media_id", "int", "Custom thumbnail"),
            Option(
                "privacy",
                "choice",
                "Privacy",
                choices=("public", "unlisted", "private"),
                default="public",
            ),
        ),
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
        options=(
            Option(FIRST_COMMENT[0], "str", FIRST_COMMENT[1], max_length=5000),
            Option(LOCATION[0], "str", LOCATION[1], max_length=64),
            Option("targeting_countries", "list[str]", "Restrict to countries"),
            Option("targeting_min_age", "int", "Minimum age"),
        ),
    ),
}


def options_for(platform: str) -> dict[str, Option]:
    """This platform's option declarations, by key.

    A platform with no rule row returns nothing rather than raising: an
    unknown platform is already refused upstream, and a validator that
    exploded here would turn a bad request into a 500.
    """
    rule = PLATFORM_RULES.get(platform)
    return {option.key: option for option in rule.options} if rule else {}
