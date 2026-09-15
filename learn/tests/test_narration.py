"""P7-08 … P7-10 — the statistics/narration split, enforced.

The rule (Part 7 rule 15) is that **statistics are computed in code and the LLM
only renders them**. A rule like that is worth nothing as a prompt instruction:
the model will comply almost always, and the digest is read as authoritative
precisely on the occasions it does not. So compliance is checked after the fact
against the payload, and a render that fails is replaced by template phrasing
rather than shown with a warning.

What makes this testable is that the failure is mechanical: a numeral in the
prose that is not in the payload cannot have come from the data.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ai.providers.base import TextGenerationResult, TextVariant
from learn.models import Confidence, NarrationSource
from learn.services import narrate, statistics


class StubProvider:
    """A `TextProvider` that says exactly what the test tells it to."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def generate(
        self, *, system: str, prompt: str, n: int, model: str | None = None
    ) -> TextGenerationResult:
        self.calls.append({"system": system, "prompt": prompt, "n": n})
        return TextGenerationResult(
            variants=[TextVariant(body=self.body)],
            provider="stub",
            model="stub-1",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )


def stat(**overrides: object) -> statistics.SegmentStat:
    base: dict[str, object] = {
        "dimension": "format",
        "value": "CAROUSEL",
        "sample_size": 20,
        "baseline_size": 40,
        "campaigns_observed": 3,
        "segment_mean": 0.08,
        "baseline_mean": 0.04,
        "led_in": 14,
        "confidence": Confidence.STRONG,
    }
    base.update(overrides)
    return statistics.SegmentStat(**base)  # type: ignore[arg-type]


def payload_for(*stats: statistics.SegmentStat) -> dict[str, object]:
    return narrate.build_payload(
        list(stats),
        window_start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        window_end=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        campaign_name="Spring launch",
    )


class TestThePayload:
    def test_carries_the_grade_and_sample_size_of_every_finding(self) -> None:
        payload = payload_for(stat(), stat(value="FEED", sample_size=9))

        graded = {row["segment"]: row for row in payload["findings"]}  # type: ignore[index]
        assert graded["CAROUSEL"]["sample_size"] == 20
        assert graded["CAROUSEL"]["confidence"] == "STRONG"
        assert graded["FEED"]["sample_size"] == 9

    def test_holds_no_prose(self) -> None:
        """The payload is the *input* to rendering, so a sentence in it would
        be a sentence nothing validated."""
        payload = payload_for(stat())
        for finding in payload["findings"]:  # type: ignore[union-attr]
            assert "narration" not in finding


class TestNumeralGrounding:
    """P7-09 — any numeral in the prose absent from the payload fails."""

    def test_a_faithful_rendering_is_kept(self) -> None:
        stats = [stat()]
        provider = StubProvider("Carousels led in 14 of 20 posts this window.")

        text, source = narrate.narrate(payload_for(*stats), provider=provider)

        assert "14 of 20" in text
        assert source == NarrationSource.MODEL

    def test_an_invented_number_falls_back_to_the_template(self) -> None:
        """The number is the tell. 34 appears nowhere in the payload, so it was
        not computed — it was produced, which is the one thing forbidden."""
        stats = [stat()]
        provider = StubProvider("Carousels reached 34 percent more people.")

        text, source = narrate.narrate(payload_for(*stats), provider=provider)

        assert source == NarrationSource.TEMPLATE
        assert "34" not in text

    def test_a_predicted_delta_falls_back_even_with_grounded_numbers(self) -> None:
        """P7-10. `+20%` is arithmetic on real figures and still forbidden:
        upstream metric quality is uneven, and a comparative claim degrades
        gracefully under bad data where a numeric promise does not."""
        stats = [stat()]
        provider = StubProvider("Switching to carousels will increase reach by 20%.")

        _, source = narrate.narrate(payload_for(*stats), provider=provider)

        assert source == NarrationSource.TEMPLATE

    @pytest.mark.parametrize(
        "forbidden",
        [
            "We expect reach to climb next month.",
            "Projected engagement is higher for carousels.",
            "Reach will improve if you post at 20 past the hour.",
            "This should rise to 20 next month.",
            "Carousels deliver +20% engagement.",
        ],
    )
    def test_every_prediction_shape_is_refused(self, forbidden: str) -> None:
        stats = [stat()]
        text, source = narrate.narrate(payload_for(*stats), provider=StubProvider(forbidden))
        assert source == NarrationSource.TEMPLATE, forbidden
        assert not narrate.reads_as_a_prediction(text), text

    def test_a_provider_failure_falls_back_rather_than_raising(self) -> None:
        """A digest with template prose is a digest. A job that dies because a
        vendor timed out loses the statistics too, which is the expensive half."""

        class Broken:
            def generate(self, **_: object) -> TextGenerationResult:
                raise RuntimeError("provider is down")

        stats = [stat()]
        text, source = narrate.narrate(payload_for(*stats), provider=Broken())

        assert source == NarrationSource.TEMPLATE
        assert text


class TestTheTemplate:
    """The fallback has to be publishable on its own — it is what a customer
    reads whenever the provider is down or wrong."""

    def test_states_the_grade_and_the_sample_size(self) -> None:
        text = narrate.template_narration(payload_for(stat()))
        assert "20" in text
        assert "carousel" in text.lower()

    def test_says_so_plainly_when_nothing_is_confident_yet(self) -> None:
        text = narrate.template_narration(
            payload_for(stat(sample_size=3, confidence=Confidence.INSUFFICIENT))
        )
        assert "not enough data" in text.lower()

    def test_never_predicts(self) -> None:
        for confidence in Confidence:
            text = narrate.template_narration(payload_for(stat(confidence=confidence)))
            assert not narrate.reads_as_a_prediction(text), text

    def test_every_numeral_it_prints_is_in_the_payload(self) -> None:
        """The fallback has to satisfy the rule it enforces, or the guard is
        circular: a template that invented a number would be trusted precisely
        because it is the thing invalid renders are replaced with."""
        payload = payload_for(stat(), stat(value="FEED", sample_size=9, led_in=2))
        assert narrate.numerals_are_grounded(narrate.template_narration(payload), payload)
