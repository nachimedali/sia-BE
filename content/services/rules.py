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

from dataclasses import dataclass, field
from typing import Any

from content.models import MediaKind, Platform, PostFormat

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
    #: `media` is not a synonym for `int`. An option that references a
    #: `MediaAsset` is a tenancy surface — an integer validator accepts another
    #: workspace's asset id and nothing downstream would notice — so the kind
    #: is what tells the validator to resolve it against the workspace.
    kind: str  # "str" | "int" | "bool" | "url" | "choice" | "list[str]" | "media"
    label: str
    #: `choice` only. Ignored otherwise rather than raising, so a row is not
    #: forced to carry a field its kind has no use for.
    choices: tuple[str, ...] = ()
    max_length: int | None = None
    #: `remote_choice` only. Names the list the composer fetches instead of
    #: drawing a text box — a Pinterest board id typed by hand is a rejected
    #: pin. Validation stays **offline**: the value is checked as a string
    #: here, because a validator that made a network call would fail a save
    #: whenever the provider was slow.
    source: str = ""
    #: What an absent key means. Never `None` for a `bool` — a tri-state
    #: boolean is how "unset" and "false" start being confused.
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class FormatRule:
    """What one `(platform, format)` pair permits (P4-05).

    **The media constraints live here, not on the platform.** A platform-level
    cap cannot say the thing that is actually true — a LinkedIn feed post takes
    nine images and its document carousel takes one PDF — so a single number
    forces every format to share the loosest of them, and the composer accepts
    a post the provider will reject at publish time, on the one path nobody is
    watching.
    """

    max_media: int
    allowed_media_kinds: frozenset[str]
    #: `None` means "inherit the platform's limit", not "no limit". A format
    #: that does not narrow the caption says nothing rather than repeating a
    #: number that would then have two places to be wrong.
    char_limit: int | None = None
    #: Formats that cannot exist without media — a reel with no video is not a
    #: reel. `0` is the honest default: a plain feed post is text alone.
    min_media: int = 0


@dataclass(frozen=True)
class PlatformRule:
    char_limit: int
    hashtag_placement: str  # "inline" | "trailing_block"
    supports_thread: bool
    #: `{format: FormatRule}` (P4-04). Non-empty, and always carrying `FEED` —
    #: the format a post has when nobody chose one. Asserted by
    #: `test_formats.py`, because a platform whose default is missing would
    #: make every target need an explicit choice the composer has no reason to
    #: ask for.
    formats: dict[str, FormatRule] = field(default_factory=dict)
    #: Per-platform composer fields (P1-11). Empty is the honest default: a
    #: platform with nothing to configure declares nothing, rather than
    #: inheriting a union of every other platform's options.
    options: tuple[Option, ...] = ()


#: The media-kind sets, named once. The table below is meant to be *read* —
#: `frozenset({MediaKind.IMAGE, MediaKind.VIDEO})` repeated nine times is noise
#: that hides the one row where it differs.
STILLS = frozenset({MediaKind.IMAGE})
MOTION = frozenset({MediaKind.VIDEO})
VISUAL = frozenset({MediaKind.IMAGE, MediaKind.VIDEO})
DOCS = frozenset({MediaKind.DOCUMENT})


PLATFORM_RULES: dict[str, PlatformRule] = {
    Platform.INSTAGRAM: PlatformRule(
        char_limit=2200,
        formats={
            PostFormat.FEED: FormatRule(max_media=10, allowed_media_kinds=VISUAL),
            PostFormat.CAROUSEL: FormatRule(max_media=10, allowed_media_kinds=VISUAL, min_media=2),
            # One video, and it must be there — a reel with no video is not a
            # reel, which is what `min_media` exists to say.
            PostFormat.REEL: FormatRule(max_media=1, allowed_media_kinds=MOTION, min_media=1),
            PostFormat.STORY: FormatRule(max_media=1, allowed_media_kinds=VISUAL, min_media=1),
        },
        hashtag_placement="trailing_block",
        supports_thread=False,
        options=(
            Option(FIRST_COMMENT[0], "str", FIRST_COMMENT[1], max_length=2200),
            Option(LOCATION[0], "str", LOCATION[1], max_length=64),
            Option("collab_handles", "list[str]", "Invite as collaborators"),
            Option("tagged_handles", "list[str]", "Tag accounts in this post"),
            Option("share_to_feed", "bool", "Also share a reel to the feed", default=True),
        ),
    ),
    Platform.LINKEDIN: PlatformRule(
        char_limit=3000,
        formats={
            PostFormat.FEED: FormatRule(max_media=9, allowed_media_kinds=VISUAL),
            PostFormat.CAROUSEL: FormatRule(max_media=9, allowed_media_kinds=STILLS, min_media=2),
            # The document post. One file, and `DOCUMENT` is its own kind
            # rather than an image: a PDF that validated as an image would be
            # rejected by the provider after passing every check here.
            PostFormat.PDF_CAROUSEL: FormatRule(max_media=1, allowed_media_kinds=DOCS, min_media=1),
        },
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
            # Organization pages only — LinkedIn closes targeting on personal
            # profiles, the same asymmetry L-3 records for reaction detail.
            Option("targeting_locales", "list[str]", "Restrict to languages"),
            Option("tagged_organization_ids", "list[str]", "Tag organisations"),
        ),
    ),
    Platform.TIKTOK: PlatformRule(
        char_limit=2200,
        formats={
            PostFormat.FEED: FormatRule(max_media=1, allowed_media_kinds=MOTION, min_media=1),
        },
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option("allow_comments", "bool", "Allow comments", default=True),
            Option("allow_duet", "bool", "Allow duet", default=True),
        ),
    ),
    Platform.YOUTUBE: PlatformRule(
        char_limit=5000,
        formats={
            PostFormat.FEED: FormatRule(max_media=1, allowed_media_kinds=MOTION, min_media=1),
            PostFormat.SHORT: FormatRule(max_media=1, allowed_media_kinds=MOTION, min_media=1),
        },
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option("title", "str", "Video title", max_length=100, required=True),
            Option("thumbnail_media_id", "media", "Custom thumbnail"),
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
        formats={
            PostFormat.FEED: FormatRule(max_media=10, allowed_media_kinds=VISUAL),
        },
        hashtag_placement="inline",
        supports_thread=True,
    ),
    Platform.FACEBOOK: PlatformRule(
        char_limit=5000,
        formats={
            PostFormat.FEED: FormatRule(max_media=10, allowed_media_kinds=VISUAL),
            PostFormat.STORY: FormatRule(max_media=1, allowed_media_kinds=VISUAL, min_media=1),
            PostFormat.REEL: FormatRule(max_media=1, allowed_media_kinds=MOTION, min_media=1),
        },
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option(FIRST_COMMENT[0], "str", FIRST_COMMENT[1], max_length=5000),
            Option(LOCATION[0], "str", LOCATION[1], max_length=64),
            Option("targeting_countries", "list[str]", "Restrict to countries"),
            Option("targeting_min_age", "int", "Minimum age"),
            Option("targeting_interests", "list[str]", "Restrict to interests"),
            Option("targeting_locales", "list[str]", "Restrict to languages"),
            Option("tagged_page_ids", "list[str]", "Tag Pages in this post"),
        ),
    ),
    # --- Phase 4 (P4-01, P4-02, P4-03) -------------------------------------
    #
    # Three platforms, three rows. Nothing below this file changed to add
    # them except the adapter's provider-field map, which is itself a table —
    # `tests/test_phase4_gates.py` is what holds that claim to account.
    Platform.X: PlatformRule(
        char_limit=280,
        formats={
            PostFormat.FEED: FormatRule(max_media=4, allowed_media_kinds=VISUAL),
        },
        hashtag_placement="inline",
        # Threading comes from this flag alone — the `(n/m)` splitter built for
        # Threads (P4-01) is reached by declaring `True`, not by adding a
        # branch for X.
        supports_thread=True,
        options=(
            Option(
                "reply_settings",
                "choice",
                "Who can reply",
                choices=("everyone", "following", "mentioned"),
                default="everyone",
            ),
        ),
    ),
    Platform.PINTEREST: PlatformRule(
        char_limit=500,
        formats={
            # A pin with no image is not a pin: on Pinterest the media *is*
            # the post and the text is its caption, which is what `min_media`
            # says here and could not be said at platform level at all.
            PostFormat.FEED: FormatRule(max_media=1, allowed_media_kinds=VISUAL, min_media=1),
        },
        hashtag_placement="inline",
        supports_thread=False,
        options=(
            Option(
                "board_id",
                "remote_choice",
                "Board",
                source="pinterest_boards",
                required=True,
            ),
            Option("title", "str", "Pin title", max_length=100),
            Option("destination_link", "url", "Where the pin links to"),
        ),
    ),
    Platform.GOOGLE_BUSINESS: PlatformRule(
        char_limit=1500,
        formats={
            PostFormat.FEED: FormatRule(max_media=1, allowed_media_kinds=VISUAL),
        },
        hashtag_placement="inline",
        supports_thread=False,
        # **The deliberate stress test** (P4-03). Offers and events are post
        # *types*, not formats: `PostFormat` describes shape — feed, story,
        # reel — and an offer is a feed post carrying extra fields, so bending
        # the format enum around it would hand every other platform two values
        # it can never use.
        options=(
            Option(
                "post_type",
                "choice",
                "Post type",
                choices=("STANDARD", "OFFER", "EVENT"),
                default="STANDARD",
            ),
            Option(
                "cta_type",
                "choice",
                "Button",
                choices=("BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL"),
            ),
            Option("cta_url", "url", "Where the button goes"),
            Option("event_title", "str", "Event title", max_length=58),
            Option("event_start", "str", "Event starts (ISO 8601)"),
            Option("event_end", "str", "Event ends (ISO 8601)"),
            Option("offer_coupon_code", "str", "Coupon code", max_length=58),
            Option("offer_redeem_url", "url", "Where to redeem"),
            Option("offer_terms", "str", "Terms and conditions", max_length=5000),
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


def formats_for(platform: str) -> dict[str, FormatRule]:
    """This platform's declared formats, by name.

    Empty for an unknown platform rather than raising — the same reasoning
    `options_for` follows: an unknown platform is already refused upstream,
    and exploding here turns a bad request into a 500.
    """
    rule = PLATFORM_RULES.get(platform)
    return dict(rule.formats) if rule else {}


def format_rule(platform: str, post_format: str) -> FormatRule | None:
    """The constraints for one `(platform, format)` pair, or `None` if this
    platform does not declare that format.

    `None` rather than a fallback to `FEED`: silently substituting a format
    the caller did not ask for is how a composer ends up offering Instagram a
    YouTube Short. The two callers that must not crash — the renderer and the
    served declarations — handle the `None` explicitly and visibly.
    """
    return formats_for(platform).get(post_format)
