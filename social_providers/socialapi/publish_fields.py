"""`render_post` output → SocialAPI's `POST /posts` body (SA-30).

**A table, not code paths** (rule 13). Each platform is one `PlatformMapping`
row: its SocialAPI slug, where its `platform_data` lives, how each of our
composer options travels, which formats need an explicit `content_type`, how
alt text is carried and how a thread is chained. Adding a platform is a row;
`social_providers/tests/test_publish_fields.py` fails when a declared option
has no row.

**Every declared option is accounted for, explicitly.** An option either maps
to one or more request paths (`To`) or is `Unsupported` with the reason. An
unsupported option the person actually set refuses the publish *before*
anything is created at the provider, with that reason: a post that went out
without the audience restriction someone chose is worse than one that did not
go out. A default value is not "set", so an untouched toggle never blocks.

**The body is built from the rendered payload and nothing else** (rule 2):
the text is the payload's body byte for byte, the media order is the payload's
order, alt text is the payload's per-use alt text. `build_request` is pure, so
`test_preview_and_publish_send_the_same_body` can hold it to that.

Placement follows each platform page of the vendor's docs (fetched
2026-10-13): Instagram, TikTok, Threads and Google read `platform_data.<slug>`;
Facebook, LinkedIn, Pinterest, YouTube and X read the target's own flat
`platform_data`. Documented, not yet observed — SA-90 is the live check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from channels.adapters.base import PlatformError
from common.timestamps import parse_or_none
from content.models import Platform, PostFormat
from content.services.rules import options_for


class _Omit:
    """A transform's way of saying "send nothing for this value"."""

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "OMIT"


OMIT = _Omit()


@dataclass(frozen=True)
class To:
    """Where one option value goes. `path` is `target.<field>` (a field of the
    request's single target) or `data.<a>.<b>` (inside `platform_data`)."""

    path: str
    transform: Callable[[Any], Any] | None = None
    #: Formats this field applies to; empty means every format. A key sent for
    #: a format that ignores it comes back as a validation warning, which the
    #: adapter treats as a dropped setting — so it is not sent at all.
    formats: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Unsupported:
    reason: str


@dataclass(frozen=True)
class AltText:
    #: `platform_data` key for one image's alt text.
    single: str = ""
    #: `platform_data` key for a list matched by position to `media`.
    many: str = ""
    formats: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlatformMapping:
    slug: str
    #: `nested` → `platform_data.<slug>.<key>`; `target` → the target's own flat
    #: `platform_data.<key>`.
    placement: str
    options: dict[str, tuple[To, ...] | Unsupported] = field(default_factory=dict)
    #: Our format → `(platform_data key, value)` that selects it at the vendor.
    #: Formats absent here are auto-detected from the media.
    formats: dict[str, tuple[str, str]] = field(default_factory=dict)
    alt_text: AltText | None = None
    #: How continuation posts of a thread travel: `segments` (top-level
    #: continuation posts), `data.<key>` (a list in platform_data), or `""`.
    thread: str = ""


def _negate(value: Any) -> bool:
    return not bool(value)


def _upper(value: Any) -> Any:
    return str(value).upper() if value else OMIT


def _date(value: Any) -> Any:
    moment = parse_or_none(value)
    return {"year": moment.year, "month": moment.month, "day": moment.day} if moment else OMIT


def _time(value: Any) -> Any:
    moment = parse_or_none(value)
    if moment is None or len(str(value)) <= 10:
        return OMIT  # a date alone carries no time of day
    return {"hours": moment.hour, "minutes": moment.minute}


def _handles(value: Any) -> Any:
    names = [str(item).lstrip("@").strip() for item in value or [] if str(item).strip()]
    return names or OMIT


def _user_tags(value: Any) -> Any:
    # Our composer stores who is tagged, not where on the image. Instagram
    # requires a position on feed posts, so the tag sits at the centre.
    names = _handles(value)
    if names is OMIT:
        return OMIT
    return [{"username": name, "x": 0.5, "y": 0.5} for name in names]


def _choice(table: dict[str, Any]) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        return table.get(str(value), OMIT)

    return convert


ALL_IMAGES = frozenset({PostFormat.FEED, PostFormat.CAROUSEL})
REELS = frozenset({PostFormat.REEL})

MAPPINGS: dict[str, PlatformMapping] = {
    Platform.INSTAGRAM: PlatformMapping(
        slug="instagram",
        placement="nested",
        options={
            "first_comment": (To("target.first_comment"),),
            "location_id": (To("data.location_id"),),
            "collab_handles": (To("data.collaborators", _handles),),
            "tagged_handles": (To("data.user_tags", _user_tags, formats=ALL_IMAGES),),
            "share_to_feed": (To("data.share_to_feed", bool, formats=REELS),),
        },
        formats={
            PostFormat.CAROUSEL: ("content_type", "carousel"),
            PostFormat.REEL: ("content_type", "reel"),
            PostFormat.STORY: ("content_type", "stories"),
        },
        alt_text=AltText(single="alt_text", formats=frozenset({PostFormat.FEED})),
    ),
    Platform.FACEBOOK: PlatformMapping(
        slug="facebook",
        placement="target",
        options={
            "first_comment": (To("target.first_comment"),),
            "location_id": (To("data.place"),),
            "targeting_countries": (
                To("data.countries", lambda v: [str(c).upper() for c in v] or OMIT),
            ),
            "targeting_min_age": (To("data.min_age"),),
            "targeting_interests": Unsupported(
                "Facebook interest targeting is not offered by the publishing provider."
            ),
            "targeting_locales": Unsupported(
                "Facebook language targeting is not offered by the publishing provider."
            ),
            "tagged_page_ids": Unsupported(
                "Tagging Pages in a Facebook post is not offered by the publishing provider."
            ),
        },
        formats={
            PostFormat.REEL: ("content_type", "reel"),
            PostFormat.STORY: ("content_type", "stories"),
        },
        alt_text=AltText(single="alt_text", formats=frozenset({PostFormat.FEED})),
    ),
    Platform.LINKEDIN: PlatformMapping(
        slug="linkedin",
        placement="target",
        options={
            "first_comment": Unsupported(
                "LinkedIn does not take a first comment through the publishing provider."
            ),
            "visibility": (
                To(
                    "target.visibility",
                    _choice({"PUBLIC": "public", "CONNECTIONS": "connections_only"}),
                ),
            ),
            "targeting_locales": Unsupported(
                "LinkedIn language targeting is not offered by the publishing provider."
            ),
            "tagged_organization_ids": Unsupported(
                "Tagging organisations on LinkedIn is not offered by the publishing provider."
            ),
        },
        alt_text=AltText(single="alt_text", many="alt_texts"),
    ),
    Platform.TIKTOK: PlatformMapping(
        slug="tiktok",
        placement="nested",
        options={
            "privacy_level": (To("data.privacy_level"),),
            "allow_comments": (To("data.disable_comment", _negate),),
            "allow_duet": (To("data.disable_duet", _negate),),
        },
    ),
    Platform.YOUTUBE: PlatformMapping(
        slug="youtube",
        placement="target",
        options={
            "title": (To("target.title"),),
            "privacy": (To("target.visibility"),),
            "thumbnail_media_id": Unsupported(
                "A custom YouTube thumbnail is not offered by the publishing provider."
            ),
        },
    ),
    Platform.THREADS: PlatformMapping(slug="threads", placement="nested", thread="segments"),
    Platform.X: PlatformMapping(
        slug="twitter",
        placement="target",
        options={
            "reply_settings": (
                To(
                    "data.reply_settings",
                    _choice({"following": "following", "mentioned": "mentionedUsers"}),
                ),
            ),
        },
        thread="data.thread",
    ),
    Platform.PINTEREST: PlatformMapping(
        slug="pinterest",
        placement="target",
        options={
            "board_id": (To("target.board_id"),),
            "title": (To("data.title"),),
            "destination_link": (To("data.link"),),
        },
        alt_text=AltText(single="alt_text"),
    ),
    Platform.GOOGLE_BUSINESS: PlatformMapping(
        slug="google",
        placement="nested",
        options={
            "post_type": (To("data.topicType", _upper),),
            "cta_type": (To("data.callToAction.actionType", _upper),),
            "cta_url": (To("data.callToAction.url"),),
            "event_title": (To("data.event.title"),),
            "event_start": (
                To("data.event.schedule.startDate", _date),
                To("data.event.schedule.startTime", _time),
            ),
            "event_end": (
                To("data.event.schedule.endDate", _date),
                To("data.event.schedule.endTime", _time),
            ),
            "offer_coupon_code": (To("data.offer.couponCode"),),
            "offer_redeem_url": (To("data.offer.redeemOnlineUrl"),),
            "offer_terms": (To("data.offer.termsConditions"),),
        },
    ),
}


def mapping_for(platform: str) -> PlatformMapping:
    try:
        return MAPPINGS[platform]
    except KeyError:
        raise PlatformError(
            f"{platform} cannot be published through this provider.",
            code="platform_not_supported",
            detail={"platform": platform},
        ) from None


def _is_set(value: Any, default: Any) -> bool:
    """Whether the person chose something, as opposed to leaving the default."""
    if value is None or value == "" or value == [] or value == {}:
        return False
    return default is None or value != default


def _put(tree: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    for part in parents:
        tree = tree.setdefault(part, {})
    tree[leaf] = value


def refusals(platform: str, payload: dict[str, Any]) -> list[str]:
    """Why this payload cannot go through the provider as composed — empty
    when it can. Checked before any call, so a refusal costs nothing."""
    declared = options_for(platform)
    options = payload.get("options") or {}
    return [
        f"{rule.reason} Remove that setting to publish."
        for key, rule in mapping_for(platform).options.items()
        if isinstance(rule, Unsupported)
        and _is_set(options.get(key), getattr(declared.get(key), "default", None))
    ]


@dataclass(frozen=True)
class BuiltRequest:
    body: dict[str, Any]
    #: The `platform_data` keys we sent — a validation warning naming one of
    #: these means the vendor would drop a setting the person chose.
    data_keys: frozenset[str]


def build_request(
    *,
    platform: str,
    provider_account_id: str,
    payload: dict[str, Any],
    media_sources: list[dict[str, str]],
) -> BuiltRequest:
    """The `POST /posts` body for one target. Pure: same input, same bytes.

    No `publish_now` and no `scheduled_at` — the post is created as a draft,
    which is free and never goes live; we keep the schedule ourselves and
    publish the draft by id (SA-32)."""
    mapping = mapping_for(platform)
    post_format = str(payload.get("post_format") or PostFormat.FEED)
    options = payload.get("options") or {}
    target: dict[str, Any] = {"account_id": provider_account_id}
    data: dict[str, Any] = {}

    for key, rule in mapping.options.items():
        if isinstance(rule, Unsupported) or key not in options:
            continue
        for destination in rule:
            if destination.formats and post_format not in destination.formats:
                continue
            value = options[key]
            if destination.transform is not None:
                value = destination.transform(value)
            if value is OMIT or value is None or value == "":
                continue
            area, _, path = destination.path.partition(".")
            _put(target if area == "target" else data, path, value)

    if post_format in mapping.formats:
        key, value = mapping.formats[post_format]
        data[key] = value

    media_items = payload.get("media") or []
    alts = [str(item.get("alt") or "") for item in media_items]
    alt = mapping.alt_text
    if alt and any(alts) and (not alt.formats or post_format in alt.formats):
        if len(alts) == 1 and alt.single:
            data[alt.single] = alts[0]
        elif len(alts) > 1 and alt.many:
            data[alt.many] = alts

    text = str(payload.get("body") or "")
    body: dict[str, Any] = {"text": text}
    thread = [str(part) for part in payload.get("thread") or []]
    if len(thread) > 1 and mapping.thread == "segments":
        body["segments"] = [{"text": part} for part in thread[1:]]
    elif len(thread) > 1 and mapping.thread.startswith("data."):
        data[mapping.thread.removeprefix("data.")] = thread[1:]

    if media_sources:
        body["media"] = media_sources
    if data:
        if mapping.placement == "nested":
            body["platform_data"] = {mapping.slug: data}
        else:
            target["platform_data"] = data
    body["targets"] = [target]
    return BuiltRequest(body=body, data_keys=frozenset(data))
