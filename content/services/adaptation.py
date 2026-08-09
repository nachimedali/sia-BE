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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from content.services.rules import PLATFORM_RULES

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


# A hashtag starts at a word boundary so "a#b" is not one. `\w` already covers
# unicode word characters under Python's default (unicode) regex mode.
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)")

# Reserves room for the " (12/12)" suffix a thread chunk gets numbered with.
_THREAD_SUFFIX_BUDGET = 8

ELLIPSIS = "…"


@dataclass(frozen=True)
class AdaptedMedia:
    id: int
    kind: str
    url: str


@dataclass(frozen=True)
class AdaptedPayload:
    platform: str
    body: str
    thread: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    media: list[AdaptedMedia] = field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The literal JSON shape stored in `PostTarget.rendered_payload` and
        returned by `/posts/preview/` — the two are asserted byte-identical by
        comparing this output, not by comparing dataclasses."""
        return {
            "platform": self.platform,
            "body": self.body,
            "thread": list(self.thread),
            "hashtags": list(self.hashtags),
            "media": [{"id": m.id, "kind": m.kind, "url": m.url} for m in self.media],
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


def _extract_hashtags(body: str) -> list[str]:
    """Order-preserving, case-preserving, de-duplicated by lowercase form."""
    seen: set[str] = set()
    hashtags: list[str] = []
    for match in _HASHTAG_RE.findall(body):
        key = match.lower()
        if key in seen:
            continue
        seen.add(key)
        hashtags.append(match)
    return hashtags


def _strip_hashtags(body: str) -> str:
    stripped = _HASHTAG_RE.sub("", body)
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
    *, master_body: str, media_assets: Sequence[MediaLike], platform: str
) -> AdaptedPayload:
    rule = PLATFORM_RULES[platform]
    hashtags = _extract_hashtags(master_body)
    warnings: list[str] = []

    working_body = master_body.strip()
    if rule.hashtag_placement == "trailing_block" and hashtags:
        working_body = _strip_hashtags(working_body)

    thread: list[str] = []
    truncated = False
    if len(working_body) > rule.char_limit:
        if rule.supports_thread:
            thread = _split_into_thread(working_body, rule.char_limit)
            body = thread[0] if thread else ""
        else:
            body = _truncate(working_body, rule.char_limit)
            truncated = True
            warnings.append(f"Body truncated to {rule.char_limit} characters for {platform}.")
    else:
        body = working_body

    if rule.hashtag_placement == "trailing_block" and hashtags:
        block = " ".join(f"#{tag}" for tag in hashtags)
        candidate = f"{body}\n\n{block}" if body else block
        if len(candidate) <= rule.char_limit:
            body = candidate
        else:
            warnings.append(
                "Hashtag block did not fit in the body; use the hashtags list separately."
            )

    allowed = [asset for asset in media_assets if asset.kind in rule.allowed_media_kinds]
    unsupported_count = len(media_assets) - len(allowed)
    if unsupported_count:
        warnings.append(
            f"{unsupported_count} asset(s) dropped: {platform} does not support that media type."
        )

    kept = allowed[: rule.max_media]
    over_cap_count = len(allowed) - len(kept)
    if over_cap_count:
        warnings.append(
            f"{over_cap_count} asset(s) dropped: {platform} allows at most {rule.max_media}."
        )

    media = [
        AdaptedMedia(id=asset.id, kind=asset.kind, url=asset.file.url if asset.file else "")
        for asset in kept
    ]

    return AdaptedPayload(
        platform=platform,
        body=body,
        thread=thread,
        hashtags=hashtags,
        media=media,
        truncated=truncated,
        warnings=warnings,
    )


def render_payloads(
    *, master_body: str, media_assets: Sequence[MediaLike], platforms: Iterable[str]
) -> dict[str, AdaptedPayload]:
    """The one mapping both `render_post` and `PostPreviewView` build — a
    platform, adapted, for each requested platform."""
    return {
        platform: adapt_for_platform(
            master_body=master_body, media_assets=media_assets, platform=platform
        )
        for platform in platforms
    }


def render_post(post: Post, platforms: Iterable[str]) -> dict[str, AdaptedPayload]:
    """The one call site `/posts/preview/` and the Phase 9 publish task share.

    Takes a persisted `Post` rather than loose fields so both callers resolve
    `master_body` and ordered media the same way — a caller that assembled its
    own media list could drift from what `Post.ordered_media()` would return.
    """
    return render_payloads(
        master_body=post.master_body, media_assets=list(post.ordered_media()), platforms=platforms
    )
