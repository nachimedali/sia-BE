"""The Adaptation Engine (design.md §8.6).

One master post becomes a per-platform payload here: truncation, thread-
splitting, media-count caps, hashtag placement. `adapt_for_platform` is the
only function that does this, and `render_post` is the only way a persisted
`Post` reaches it. `PostPreviewView` (Phase 4) and the publish task Phase 9
adds both call `render_post` — one call path, so preview can never drift from
what actually gets sent (design.md: "the preview output must be byte-identical
to what publish sends").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from common.text import HASHTAG_RE
from content.models import ContentKind, PostFormat
from content.services.options import resolve as resolve_options
from content.services.rules import PLATFORM_RULES, format_rule

if TYPE_CHECKING:
    from content.models import Post


class MediaLike(Protocol):
    """What the engine actually reads off a media object — not `MediaAsset`
    itself, so a unit test can pass a plain stand-in instead of hitting the
    database or storage for something that never touches either.

    `file` is untyped: Django's `FieldFile` and a test double satisfy this
    structurally but not invariantly (Protocol attribute matching wants an
    exact type), and the engine only ever does `.file.url if asset.file`, so
    nothing is gained by pinning it down further.
    """

    id: int
    kind: str
    file: Any


# Reserves room for the " (12/12)" suffix a thread chunk gets numbered with.
_THREAD_SUFFIX_BUDGET = 8

ELLIPSIS = "…"


@dataclass(frozen=True)
class AdaptedMedia:
    id: int
    kind: str
    url: str
    #: Always a string, never null and never absent (P1-06). An image with no
    #: alt text renders `""`; a consumer that has to tell three states of a
    #: string apart is one that will eventually get one of them wrong.
    alt: str = ""


@dataclass(frozen=True)
class AdaptedPayload:
    platform: str
    body: str
    #: Which shape this was rendered as (P4-04). On the payload rather than
    #: derived by the reader, because the constraints that produced this body
    #: and this media list came from the `(platform, format)` row — a consumer
    #: that had to re-derive it could disagree with what was actually applied.
    post_format: str = PostFormat.FEED
    thread: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    media: list[AdaptedMedia] = field(default_factory=list)
    #: Per-platform composer settings, resolved against `rules.py` (P1-11):
    #: declared defaults filled in, undeclared or unusable keys dropped. Always
    #: present, `{}` for a platform with nothing to configure.
    options: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The literal JSON shape stored in `PostTarget.rendered_payload` and
        returned by `/posts/preview/` — the two are asserted byte-identical by
        comparing this output, not by comparing dataclasses."""
        return {
            "platform": self.platform,
            "post_format": self.post_format,
            "body": self.body,
            "thread": list(self.thread),
            "hashtags": list(self.hashtags),
            "media": [{"id": m.id, "kind": m.kind, "url": m.url, "alt": m.alt} for m in self.media],
            "options": dict(self.options),
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


def _extract_hashtags(body: str) -> list[str]:
    """Order-preserving, case-preserving, de-duplicated by lowercase form."""
    seen: set[str] = set()
    hashtags: list[str] = []
    for match in HASHTAG_RE.findall(body):
        key = match.lower()
        if key in seen:
            continue
        seen.add(key)
        hashtags.append(match)
    return hashtags


def _strip_hashtags(body: str) -> str:
    stripped = HASHTAG_RE.sub("", body)
    # Collapse the double space, or blank line, a removed hashtag leaves behind.
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"[ \t]+(?=\n|$)", "", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        return text[:limit]
    return text[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _split_into_thread(text: str, limit: int) -> list[str]:
    """Splits on word boundaries into numbered chunks, each within `limit`
    once its " (i/n)" suffix is counted. A single word too long for a chunk on
    its own is hard-split rather than left overflowing the limit."""
    max_chunk = max(1, limit - _THREAD_SUFFIX_BUDGET)
    chunks: list[str] = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chunk:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(word) > max_chunk:
            chunks.append(word[:max_chunk])
            word = word[max_chunk:]
        current = word

    if current:
        chunks.append(current)

    total = len(chunks)
    return [f"{chunk} ({i}/{total})" for i, chunk in enumerate(chunks, start=1)]


def adapt_for_platform(
    *,
    master_body: str,
    media_assets: Sequence[MediaLike],
    platform: str,
    post_format: str = PostFormat.FEED,
    alt_text: Mapping[int, str] | None = None,
    options: Mapping[str, Any] | None = None,
    workspace: Any = None,
) -> AdaptedPayload:
    """`alt_text` maps media id → description for *this* target. Passed in
    rather than read off the asset because alt text is a property of the use,
    not of the file (P1-06) — the engine is handed the resolution, it does not
    perform it.

    `options` is the **stored** `platform_options`, resolved here rather than by
    the caller. That keeps P1-04's rule intact for the third overridable thing:
    body, media and now settings all become what publish sends in one place."""
    rule = PLATFORM_RULES[platform]
    hashtags = _extract_hashtags(master_body)
    warnings: list[str] = []

    # **Resolved from the `(platform, format)` row, not from the platform**
    # (P4-05). An undeclared format falls back to `FEED` *and says so*, rather
    # than raising: by the time the renderer runs, refusing would turn a
    # preview into a 500, and `PostTarget.clean` is where an unsupported
    # format is actually refused — at the write, where it can be fixed.
    spec = format_rule(platform, post_format)
    if spec is None:
        warnings.append(f"{platform} does not support the {post_format} format; rendered as FEED.")
        post_format = PostFormat.FEED
        spec = format_rule(platform, PostFormat.FEED)
    assert spec is not None  # every platform declares FEED; test_formats.py holds it
    char_limit = spec.char_limit or rule.char_limit

    working_body = master_body.strip()
    if rule.hashtag_placement == "trailing_block" and hashtags:
        working_body = _strip_hashtags(working_body)

    thread: list[str] = []
    truncated = False
    if len(working_body) > char_limit:
        if rule.supports_thread:
            thread = _split_into_thread(working_body, char_limit)
            body = thread[0] if thread else ""
        else:
            body = _truncate(working_body, char_limit)
            truncated = True
            warnings.append(f"Body truncated to {char_limit} characters for {platform}.")
    else:
        body = working_body

    if rule.hashtag_placement == "trailing_block" and hashtags:
        block = " ".join(f"#{tag}" for tag in hashtags)
        candidate = f"{body}\n\n{block}" if body else block
        if len(candidate) <= char_limit:
            body = candidate
        else:
            warnings.append(
                "Hashtag block did not fit in the body; use the hashtags list separately."
            )

    allowed = [asset for asset in media_assets if asset.kind in spec.allowed_media_kinds]
    unsupported_count = len(media_assets) - len(allowed)
    if unsupported_count:
        warnings.append(
            f"{unsupported_count} asset(s) dropped: {platform} does not support that media type."
        )

    kept = allowed[: spec.max_media]
    over_cap_count = len(allowed) - len(kept)
    if over_cap_count:
        warnings.append(
            f"{over_cap_count} asset(s) dropped: {platform} allows at most "
            f"{spec.max_media} for a {post_format.lower()} post."
        )

    descriptions = alt_text or {}
    media = [
        AdaptedMedia(
            id=asset.id,
            kind=asset.kind,
            url=asset.file.url if asset.file else "",
            alt=descriptions.get(asset.id, ""),
        )
        for asset in kept
    ]

    if len(kept) < spec.min_media:
        # A reel with no video is not a reel. Warned rather than raised for the
        # same reason as the format fallback above — the composer shows this
        # before anyone schedules, which is when it can still be fixed.
        warnings.append(
            f"A {post_format.lower()} post on {platform} needs at least "
            f"{spec.min_media} media item(s); this has {len(kept)}."
        )

    return AdaptedPayload(
        platform=platform,
        post_format=post_format,
        body=body,
        thread=thread,
        hashtags=hashtags,
        media=media,
        options=resolve_options(platform, dict(options or {}), workspace=workspace),
        truncated=truncated,
        warnings=warnings,
    )


def render_payloads(
    *,
    master_body: str,
    media_assets: Sequence[MediaLike],
    platforms: Iterable[str],
    alt_text: Mapping[int, str] | None = None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    workspace: Any = None,
) -> dict[str, AdaptedPayload]:
    """The one mapping both `render_post` and `PostPreviewView` build — a
    platform, adapted, for each requested platform."""
    per_platform = options or {}
    return {
        platform: adapt_for_platform(
            master_body=master_body,
            media_assets=media_assets,
            platform=platform,
            alt_text=alt_text,
            options=per_platform.get(platform),
            workspace=workspace,
        )
        for platform in platforms
    }


def render_post(post: Post, platforms: Iterable[str]) -> dict[str, AdaptedPayload]:
    """The one call site `/posts/preview/` and the publish task share.

    Takes a persisted `Post` rather than loose fields so both callers resolve
    `master_body` and ordered media the same way — a caller that assembled its
    own media list could drift from what `Post.ordered_media()` would return.

    **Per-platform overrides are resolved here and nowhere else** (P1-04).
    That is the whole of the rule: the moment a target can carry its own body
    and its own media, there are two plausible places to apply them — the
    preview view and the publish task — and two places is one too many. A
    caller passes a platform and receives what will be sent; it is never handed
    the parts and asked to assemble them.
    """
    # **A DOC early-returns rather than being routed around** (P3-01). Callers
    # keep going through the one renderer and are told there is nothing to
    # adapt; the alternative — every call site checking `content_kind` first —
    # is exactly the second decision point Part 7 rule 1 exists to forbid, and
    # the first caller to forget it would render a JSON block list as a caption.
    if post.content_kind == ContentKind.DOC:
        return {}

    # `post.media_attachments.all()` evaluated exactly **once**, not through
    # `Post.ordered_attachments()` — that helper does its own `.all()`
    # internally, and calling it here and then reading `media_attachments`
    # again for the override rows would be two evaluations of the same
    # relation. Prefetched, Django's cache would absorb the second one; not
    # prefetched (`scheduling.publishing.build_targets` loads a plain `Post`
    # with no such prefetch), it is a second query. Fetching once and
    # partitioning in Python holds the "one query" property either way.
    all_attachments = list(post.media_attachments.all())
    attachments = [a for a in all_attachments if a.target_override_id is None]
    assets = [attachment.media_asset for attachment in attachments]
    by_id = {asset.id: asset for asset in assets}
    base_alt = {a.media_asset_id: a.alt_text for a in attachments}
    target_alt: dict[int, dict[int, str]] = {}
    for attachment in all_attachments:
        if attachment.target_override_id is not None:
            target_alt.setdefault(attachment.target_override_id, {})[attachment.media_asset_id] = (
                attachment.alt_text
            )

    # One query for the whole render rather than one per platform: preview asks
    # for every platform at once, and a six-platform post should not cost six
    # round-trips to answer a question about itself.
    overrides = {target.platform: target for target in post.targets.all()}

    payloads: dict[str, AdaptedPayload] = {}
    for platform in platforms:
        target = overrides.get(platform)
        payloads[platform] = adapt_for_platform(
            master_body=_resolved_body(post, target),
            media_assets=_resolved_media(assets, by_id, target),
            platform=platform,
            # The target's own format, resolved here with every other
            # override (P1-04): body, media, options and now shape all become
            # what publish sends in exactly one place. A platform with no
            # target yet — preview before scheduling — renders as `FEED`.
            post_format=target.post_format if target is not None else PostFormat.FEED,
            alt_text=_resolved_alt_text(base_alt, target_alt, target),
            options=target.platform_options if target is not None else {},
            workspace=post.workspace,
        )
    return payloads


def _resolved_body(post: Post, target: Any) -> str:
    """The master body, or this target's override.

    **`None` and `""` are different.** Null means inherit; an empty string is a
    deliberately empty caption, which is legitimate on a video-first platform.
    Collapsing them with `or` — the obvious one-liner — makes an empty caption
    unrepresentable and makes clearing an override impossible.
    """
    if target is None or target.body_override is None:
        return str(post.master_body)
    return str(target.body_override)


def _resolved_media(
    assets: Sequence[MediaLike], by_id: Mapping[int, MediaLike], target: Any
) -> list[MediaLike]:
    """The post's ordered media, or this target's own selection.

    The override is a list of ids rather than of assets, so it cannot smuggle
    in media from another workspace: an id that is not already on the post
    simply is not found. Order is the override's, because choosing a different
    order is most of why anyone overrides media at all.
    """
    if target is None or target.media_override is None:
        return list(assets)
    return [by_id[asset_id] for asset_id in target.media_override if asset_id in by_id]


def _resolved_alt_text(
    base: Mapping[int, str], per_target: Mapping[int, Mapping[int, str]], target: Any
) -> dict[int, str]:
    """The post-level description, or this target's own.

    An **empty** override falls back to the base text rather than publishing a
    blank description: unlike a body, where "" is a deliberately empty caption,
    a blank alt text is never a choice anybody makes on purpose — it is the
    field being cleared. Clearing an override means "use what the post says",
    which is the only reading that lets a user undo a per-platform edit.
    """
    resolved = dict(base)
    if target is None:
        return resolved
    for asset_id, text in per_target.get(target.id, {}).items():
        if text:
            resolved[asset_id] = text
    return resolved
