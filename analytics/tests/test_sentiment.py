"""Comment sentiment (design.md §6.7).

The lexicon is a shortcut for the obvious cases; the provider handles the rest.
Both halves are covered here, plus the degradation path — a sentiment failure
must never cost the metrics captured in the same pass.
"""

from __future__ import annotations

from typing import Any

import pytest

from analytics.models import Sentiment
from analytics.services.sentiment import classify_comments

pytestmark = pytest.mark.django_db


def test_the_lexicon_answers_the_obvious_cases_without_a_provider_call() -> None:
    from ai.providers.fake import _fake_text_provider

    _fake_text_provider.clear()

    verdicts = classify_comments(["love this", "terrible", "ok", ""])

    assert [v.sentiment for v in verdicts] == [
        Sentiment.POSITIVE,
        Sentiment.NEGATIVE,
        Sentiment.NEUTRAL,
        Sentiment.NEUTRAL,
    ]
    assert _fake_text_provider.calls == []


def test_a_long_comment_goes_to_the_provider(monkeypatch: Any) -> None:
    """Above four words, negation and sarcasm live — a word list is more likely
    to be wrong than right, so the model decides."""
    long_comment = "I really did not think this would be as good as it is"

    monkeypatch.setattr(
        "analytics.services.sentiment.get_text_provider",
        lambda: _provider_returning("POSITIVE"),
    )

    verdicts = classify_comments([long_comment])

    assert verdicts[0].sentiment == Sentiment.POSITIVE
    assert verdicts[0].score == 1.0


def test_a_mixed_short_comment_goes_to_the_provider(monkeypatch: Any) -> None:
    """"love it, but awful packaging" carries both markers in four words; a
    count cannot call that, so it is escalated rather than guessed."""
    monkeypatch.setattr(
        "analytics.services.sentiment.get_text_provider",
        lambda: _provider_returning("NEGATIVE"),
    )

    verdicts = classify_comments(["love but awful"])

    assert verdicts[0].sentiment == Sentiment.NEGATIVE


def test_verdicts_come_back_in_the_order_they_were_asked(monkeypatch: Any) -> None:
    """The caller pairs verdicts to comments positionally, so a shuffled or
    partial answer must not attach one comment's sentiment to another."""
    monkeypatch.setattr(
        "analytics.services.sentiment.get_text_provider",
        lambda: _provider_returning("NEGATIVE\nPOSITIVE"),
    )

    verdicts = classify_comments(
        [
            "this is the worst thing I have ever bought online",
            "love this",  # handled by the lexicon, not the provider
            "genuinely one of the best purchases I have made this year",
        ]
    )

    assert [v.sentiment for v in verdicts] == [
        Sentiment.NEGATIVE,
        Sentiment.POSITIVE,
        Sentiment.POSITIVE,
    ]


def test_an_unreadable_label_falls_back_to_neutral(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "analytics.services.sentiment.get_text_provider",
        lambda: _provider_returning("I think it is quite nice actually"),
    )

    verdicts = classify_comments(["a comment long enough to reach the provider path"])

    assert verdicts[0].sentiment == Sentiment.NEUTRAL


def test_a_provider_failure_degrades_to_neutral_rather_than_raising(
    monkeypatch: Any,
) -> None:
    """A missing sentiment costs one comment's contribution to an aggregate; a
    raised exception would lose the whole capture, including the metrics — and
    a metric for a moment that has passed cannot be re-fetched."""
    from common.exceptions import ProviderError

    class Exploding:
        def generate(self, **_kwargs: Any) -> Any:
            raise ProviderError("model down")

    monkeypatch.setattr("analytics.services.sentiment.get_text_provider", lambda: Exploding())

    verdicts = classify_comments(["a comment long enough to reach the provider path"])

    assert verdicts[0].sentiment == Sentiment.NEUTRAL
    assert verdicts[0].score == 0.0


def _provider_returning(body: str) -> Any:
    from ai.providers.base import TextGenerationResult, TextVariant

    class Stub:
        def generate(self, **_kwargs: Any) -> TextGenerationResult:
            return TextGenerationResult(
                variants=[TextVariant(body=body)],
                provider="stub",
                model="stub",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
            )

    return Stub()
