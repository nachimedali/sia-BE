"""Tokenisation shared by everything that compares two pieces of writing.

Three callers, one notion of what a word is: `trends.services.normalize`
fingerprints an item's vocabulary to catch reposts, `trends.services.clustering`
names a cluster after the words its members share, and the fake embedding
provider hashes tokens into a vector. They must agree — a dedup pass that counts
"the" and a label that ignores it would disagree about what two posts have in
common, and the disagreement would only ever show up as a strange cluster.

`content_tokens` drops function words, which is the whole difference between
"these two posts use English" and "these two posts are about the same thing".
"""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
        "for", "from", "had", "has", "have", "he", "her", "his", "how", "i",
        "in", "is", "it", "its", "me", "my", "not", "of", "on", "one", "or",
        "our", "out", "she", "so", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "to", "too", "us", "was",
        "we", "were", "what", "when", "which", "who", "why", "will", "with",
        "you", "your",
    ]
)

_WORD = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> list[str]:
    """Every word, lowercased. Function words included — length and shape
    checks want the real count."""
    return _WORD.findall(text.lower())


def content_words(words: list[str]) -> list[str]:
    """The subset of an already-tokenized list that carries meaning: no
    stopwords, nothing shorter than three characters (which at this point is
    mostly noise and abbreviation).

    Split from `content_tokens` so a caller that also needs the raw token list
    — `trends.services.normalize`'s spam heuristic wants the full word count,
    the fingerprint wants only these — filters once rather than tokenizing the
    same text twice with two call sites that have to agree on the predicate."""
    return [token for token in words if len(token) > 2 and token not in STOPWORDS]


def content_tokens(text: str) -> list[str]:
    """The words that carry the meaning: no stopwords, nothing shorter than
    three characters (which at this point is mostly noise and abbreviation)."""
    return content_words(tokens(text))
