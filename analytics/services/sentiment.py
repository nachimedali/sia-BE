"""Comment sentiment (design.md §6.7, §8.9).

Delegated to `TextProvider.classify_constraints`' sibling capability —
design.md §5 describes the LLM provider's job as "text, labelling, synthesis",
and this is the labelling half again, the same reasoning that kept the quality
gate's brand-constraint check off a second vision-specific port.

**Batched, and lexically pre-filtered.** Most comments are three words long
("love this", "🔥🔥", "where can I buy") and a model call to classify them is
waste. The lexicon handles the unambiguous ones for free and only what is left
goes to the provider — which on a real post is a small fraction, and on a post
with a thousand one-word comments is almost none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai.providers.llm_text import get_text_provider
from analytics.models import Sentiment
from common.exceptions import ProviderError
from common.text import tokens

logger = logging.getLogger(__name__)

#: Unambiguous single words. Deliberately small: this is a shortcut for the
#: obvious cases, not an attempt to do sentiment analysis with a word list.
_POSITIVE = frozenset(
    [
        "love",
        "loved",
        "lovely",
        "great",
        "amazing",
        "awesome",
        "perfect",
        "beautiful",
        "gorgeous",
        "stunning",
        "brilliant",
        "excellent",
        "fantastic",
        "wonderful",
        "nice",
        "good",
        "best",
        "obsessed",
        "yes",
        "wow",
        "incredible",
    ]
)
_NEGATIVE = frozenset(
    [
        "hate",
        "hated",
        "awful",
        "terrible",
        "horrible",
        "worst",
        "ugly",
        "disappointing",
        "disappointed",
        "scam",
        "fake",
        "overpriced",
        "rubbish",
        "bad",
        "boring",
        "waste",
    ]
)

#: Above this many words, a comment is complex enough that a word list is more
#: likely to be wrong than right — negation, sarcasm and comparison all live
#: above it — so it goes to the provider.
LEXICON_MAX_WORDS = 4


@dataclass(frozen=True)
class SentimentVerdict:
    sentiment: str
    score: float


NEUTRAL = SentimentVerdict(Sentiment.NEUTRAL, 0.0)


def _from_lexicon(body: str) -> SentimentVerdict | None:
    """A verdict for the obvious cases, or `None` to ask the provider."""
    words = tokens(body)
    if not words:
        return NEUTRAL
    if len(words) > LEXICON_MAX_WORDS:
        return None

    positive = sum(word in _POSITIVE for word in words)
    negative = sum(word in _NEGATIVE for word in words)
    if positive and not negative:
        return SentimentVerdict(Sentiment.POSITIVE, 1.0)
    if negative and not positive:
        return SentimentVerdict(Sentiment.NEGATIVE, -1.0)
    if positive or negative:
        # Both present in four words or fewer is a mixed message; the provider
        # is better placed to call it than a count is.
        return None
    return NEUTRAL


_LABELS = {"POSITIVE": Sentiment.POSITIVE, "NEGATIVE": Sentiment.NEGATIVE}
_SCORES = {Sentiment.POSITIVE: 1.0, Sentiment.NEGATIVE: -1.0, Sentiment.NEUTRAL: 0.0}

_PROMPT = (
    "Classify the sentiment of each numbered comment about a product post. "
    "Answer with one line per comment, in order, each line exactly one of: "
    "POSITIVE, NEUTRAL, NEGATIVE. No other text."
)


def classify_comments(bodies: list[str]) -> list[SentimentVerdict]:
    """One verdict per input, in order.

    A provider failure degrades to neutral rather than raising: a missing
    sentiment costs one comment's contribution to an aggregate, while a raised
    exception would lose the whole capture — including the metrics, which are
    the part that cannot be re-fetched for a moment that has passed.
    """
    verdicts: list[SentimentVerdict | None] = [_from_lexicon(body) for body in bodies]
    pending = [index for index, verdict in enumerate(verdicts) if verdict is None]
    if not pending:
        return [verdict or NEUTRAL for verdict in verdicts]

    numbered = "\n".join(f"{n + 1}. {bodies[index]}" for n, index in enumerate(pending))
    try:
        result = get_text_provider().generate(system=_PROMPT, prompt=numbered, n=1)
        lines = [
            line.strip().upper() for line in result.variants[0].body.splitlines() if line.strip()
        ]
    except (ProviderError, IndexError):
        logger.warning("sentiment classification failed; defaulting to neutral", exc_info=True)
        lines = []

    for offset, index in enumerate(pending):
        label = lines[offset] if offset < len(lines) else ""
        sentiment = _LABELS.get(label, Sentiment.NEUTRAL)
        verdicts[index] = SentimentVerdict(sentiment, _SCORES[sentiment])

    return [verdict or NEUTRAL for verdict in verdicts]
