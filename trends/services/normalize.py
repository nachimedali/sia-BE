"""Stage 2 — normalize (design.md §8.4): language filter, near-duplicate
removal, spam heuristics.

Nothing is deleted. An item that fails a check gets an `excluded_reason` and
stays, for two reasons: a re-ingest of the same `external_id` would otherwise
resurrect it as new on every run, and a heuristic that starts over-rejecting is
only visible if the rejections are still there to count.

**Near-duplicate detection is lexical, not semantic**, and deliberately so. It
runs before embeddings are computed — reposts and cross-posted ad variants are
the same *words*, and catching them with a token-set hash costs one pass and no
provider call. Semantic similarity is stage 4's job, where near-but-not-identical
items are supposed to end up in the same cluster rather than be thrown away.
"""

from __future__ import annotations

import hashlib

from common.text import content_words, tokens
from trends.models import TrendItem

EXCLUDED_LANGUAGE = "language"
EXCLUDED_DUPLICATE = "duplicate"
EXCLUDED_SPAM = "spam"

#: v1 is English-only: the prompt assembly that consumes these clusters writes
#: English, and a French exemplar grounding an English caption is noise. Widening
#: this is a per-workspace language setting, not a heuristic change.
ACCEPTED_LANGUAGES = frozenset({"", "en"})

#: Content words needed to be worth clustering. Below this a "trend" is a
#: hashtag, and an embedding of three words is dominated by whichever one
#: happens to be unusual rather than by what the post is about.
MIN_BODY_TOKENS = 4

_SPAM_MARKERS = (
    "link in bio",
    "click the link",
    "dm me",
    "free giveaway",
    "follow for follow",
)


def _shingle_hash(words: list[str]) -> str:
    """Order-insensitive fingerprint of an item's vocabulary. Two posts that
    reorder the same words — which is what a repost or an ad variant does — hash
    the same; a genuinely different post does not. Content words only, so a
    reworded caption is *not* caught here: that is stage 4's job, where it
    becomes a cluster member rather than a discarded row."""
    return hashlib.blake2b(" ".join(sorted(set(words))).encode(), digest_size=16).hexdigest()


def _is_spam(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _SPAM_MARKERS):
        return True
    # Shouting: a body that is mostly capitals and mostly exclamation is not a
    # craft signal, whatever it says.
    letters = [character for character in text if character.isalpha()]
    if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return True
    return text.count("!") >= 3 and len(words) < 20


def normalize(items: list[TrendItem]) -> list[TrendItem]:
    """Marks every item in the window and returns the ones that survived.

    Order matters: language first (cheapest, and a non-English near-duplicate
    should be reported as the language rejection it is), then spam, then dedup —
    so the copy that is kept is a real post rather than whichever repost bot
    happened to be seen first.
    """
    kept: list[TrendItem] = []
    seen: dict[str, TrendItem] = {}
    updated: list[TrendItem] = []

    for item in sorted(items, key=lambda i: (-i.author_followers, i.posted_at)):
        # Tokenized once: `_is_spam`'s shape check wants the raw word count
        # (function words included), the fingerprint wants only content words —
        # both are cheap to derive from one regex pass rather than two.
        all_words = tokens(item.body)
        meaningful = content_words(all_words)
        reason = ""
        if item.lang not in ACCEPTED_LANGUAGES:
            reason = EXCLUDED_LANGUAGE
        elif len(meaningful) < MIN_BODY_TOKENS or _is_spam(item.body, all_words):
            reason = EXCLUDED_SPAM
        else:
            fingerprint = _shingle_hash(meaningful)
            if fingerprint in seen:
                reason = EXCLUDED_DUPLICATE
            else:
                seen[fingerprint] = item

        if item.excluded_reason != reason:
            item.excluded_reason = reason
            updated.append(item)
        if not reason:
            kept.append(item)

    if updated:
        TrendItem.objects.bulk_update(updated, ["excluded_reason"])
    return kept
