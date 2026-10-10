"""Deterministic statistics (P7-04 … P7-07).

**Every number a digest contains is computed here, in Python.** Part 7 rule 15,
and the single most important constraint in the phase: the narrator downstream
receives the output of this module and may not add to it. Nothing in this file
knows what a sentence is.

Two decisions worth stating outright, because both look like over-caution until
the first time they are not:

*A segment is compared against **everything else in the window**, not against
the campaign it sits in.* P7-05. A two-week campaign at three posts a week is
n=6, under even the Emerging floor, so a digest whose statistics stopped at the
campaign boundary would read "Insufficient" on every line from the day it
shipped and be indistinguishable from a broken feature.

*An unmeasured post is absent, never a zero.* C-07 and Part 7 rule 12. Averaging
a null as 0.0 pulls a segment's mean down in proportion to how *unmeasurable* it
was, which makes the best-instrumented platform look like the best-performing
one — a wrong answer that is stable, plausible and self-reinforcing.
"""

from __future__ import annotations

import datetime as dt
import statistics as _stdlib
from dataclasses import dataclass, field

from learn.models import Confidence

#: P7-06's published bars. Named rather than inlined so the grade boundaries
#: are greppable from the digest a customer is reading.
STRONG_MIN_SAMPLE = 20
EMERGING_MIN_SAMPLE = 8
STRONG_MIN_CAMPAIGNS = 2

#: How far from the baseline an effect must sit to count as an effect, in
#: baseline standard deviations.
#:
#: **A local stand-in for Phase 8's cohort noise band.** BUILD-PLAN grades
#: Strong against the cohort, which does not exist yet; until it does, a
#: workspace's own spread is the only honest reference available. The constant
#: is the seam — Phase 8 replaces the *source* of the band, not the rule.
NOISE_BAND_SDS = 0.5

#: A baseline of one value has zero spread, which would make the noise band
#: zero-wide and every difference — including a rounding artefact — clear it.
#: The floor is expressed relative to the baseline mean so it scales with the
#: platform: 2% of a 4% engagement rate is noise, 2% of a 0.4% one is not.
MIN_NOISE_BAND_FRACTION = 0.10


@dataclass(frozen=True)
class Observation:
    """One published post, reduced to what the statistics read.

    `engagement_rate` is `None` when the metric was not measured. It is a
    separate state from zero on purpose and is carried all the way down here so
    that no intermediate layer has to remember the difference.
    """

    target_id: int
    post_id: int
    published_at: dt.datetime
    engagement_rate: float | None
    campaign_id: int | None
    dimensions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentStat:
    """One segment's standing against its baseline. Numbers only."""

    dimension: str
    value: str
    sample_size: int
    baseline_size: int
    campaigns_observed: int
    segment_mean: float
    baseline_mean: float
    #: Posts in the segment that beat the baseline mean. **A count, not a
    #: ratio** — the digest says "7 of your last 9", and a percentage computed
    #: here and multiplied back out downstream is how a rounded figure becomes
    #: a claim about posts that never existed.
    led_in: int
    confidence: Confidence
    excluded_reason: str = ""

    def as_payload(self) -> dict[str, object]:
        """The comparison, as the digest stores and the narrator receives it."""
        return {
            "segment_mean": round(self.segment_mean, 6),
            "baseline_mean": round(self.baseline_mean, 6),
            "sample_size": self.sample_size,
            "baseline_size": self.baseline_size,
            "led_in": self.led_in,
            "campaigns_observed": self.campaigns_observed,
        }


def grade(sample_size: int, *, campaigns_observed: int, clears_noise_band: bool) -> Confidence:
    """P7-06, as a pure function of three inputs.

    Strong is the only grade that may produce a **rule**, so it carries all
    three conditions: enough posts, an effect outside the noise, and persistence
    across more than one campaign. Emerging may produce a **test** proposal and
    never a rule — an effect seen inside a single campaign encodes that
    campaign's season and offer as if they were the brand's permanent physics.
    """
    if sample_size < EMERGING_MIN_SAMPLE:
        return Confidence.INSUFFICIENT
    if (
        sample_size >= STRONG_MIN_SAMPLE
        and campaigns_observed >= STRONG_MIN_CAMPAIGNS
        and clears_noise_band
    ):
        return Confidence.STRONG
    return Confidence.EMERGING


def _measured(observations: list[Observation]) -> list[Observation]:
    return [row for row in observations if row.engagement_rate is not None]


def _noise_band(values: list[float], mean: float) -> float:
    """Half-width of the band a difference must clear to count as an effect."""
    spread = _stdlib.pstdev(values) if len(values) > 1 else 0.0
    floor = abs(mean) * MIN_NOISE_BAND_FRACTION
    return max(spread * NOISE_BAND_SDS, floor)


def analyse(observations: list[Observation]) -> list[SegmentStat]:
    """Every segment in the window, compared against the rest of the window.

    Segments are enumerated from the data rather than from a declared list, so
    a dimension the segmenter stops emitting disappears from the digest instead
    of appearing as an empty section.
    """
    measured = _measured(observations)

    buckets: dict[tuple[str, str], list[Observation]] = {}
    for row in measured:
        for dimension, value in row.dimensions.items():
            if value:
                buckets.setdefault((dimension, value), []).append(row)

    stats: list[SegmentStat] = []
    for (dimension, value), segment in sorted(buckets.items()):
        # The baseline is every *other* measured post in the window that also
        # carries this dimension. Excluding the segment itself matters at small
        # n: a segment that is most of the window would otherwise be compared
        # largely against itself and reliably look average.
        rest = [
            row
            for row in measured
            if row.dimensions.get(dimension) and row.dimensions[dimension] != value
        ]

        segment_values = [row.engagement_rate for row in segment if row.engagement_rate is not None]
        segment_mean = _stdlib.fmean(segment_values)

        if not rest:
            stats.append(
                SegmentStat(
                    dimension=dimension,
                    value=value,
                    sample_size=len(segment),
                    baseline_size=0,
                    campaigns_observed=len({row.campaign_id for row in segment}),
                    segment_mean=segment_mean,
                    baseline_mean=0.0,
                    led_in=0,
                    confidence=Confidence.INSUFFICIENT,
                    # Surfaced, not dropped: "we could not compare this" and
                    # "this did not happen" are different facts, and only one
                    # of them is the customer's problem to fix.
                    excluded_reason="nothing else in the window to compare against",
                )
            )
            continue

        baseline_values = [row.engagement_rate for row in rest if row.engagement_rate is not None]
        baseline_mean = _stdlib.fmean(baseline_values)
        band = _noise_band(baseline_values, baseline_mean)

        stats.append(
            SegmentStat(
                dimension=dimension,
                value=value,
                sample_size=len(segment),
                baseline_size=len(rest),
                campaigns_observed=len({row.campaign_id for row in segment}),
                segment_mean=segment_mean,
                baseline_mean=baseline_mean,
                led_in=sum(1 for value_ in segment_values if value_ > baseline_mean),
                confidence=grade(
                    len(segment),
                    campaigns_observed=len({row.campaign_id for row in segment}),
                    clears_noise_band=abs(segment_mean - baseline_mean) >= band,
                ),
            )
        )

    return stats
