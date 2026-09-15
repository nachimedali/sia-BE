"""P7-04 … P7-07 — segmentation, trailing baselines and confidence grading.

These are the numbers every sentence in a digest is rendered from, so they are
tested as arithmetic rather than through the API: a grade that is wrong here is
wrong in a document a customer acts on, and no amount of careful prose upstream
recovers it.

The boundaries get their own cases because off-by-one is the whole risk. `n=7`
and `n=8` are different grades; `n=19` and `n=20` are different grades; and an
effect inside the noise band is not an effect however large the sample is.
"""

from __future__ import annotations

import datetime as dt

import pytest

from learn.models import Confidence
from learn.services import statistics


def rows(
    *,
    dimension: str = "format",
    value: str = "CAROUSEL",
    n: int,
    mean: float,
    campaigns: int = 2,
) -> list[statistics.Observation]:
    """`n` observations in one segment, all at `mean`, spread over `campaigns`."""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return [
        statistics.Observation(
            target_id=index,
            post_id=index,
            published_at=start + dt.timedelta(days=index),
            engagement_rate=mean,
            campaign_id=index % campaigns,
            dimensions={dimension: value},
        )
        for index in range(n)
    ]


def baseline(*, n: int, mean: float, spread: float = 0.0) -> list[statistics.Observation]:
    """A comparison population, alternating either side of `mean` by `spread`."""
    start = dt.datetime(2025, 6, 1, tzinfo=dt.UTC)
    return [
        statistics.Observation(
            target_id=1000 + index,
            post_id=1000 + index,
            published_at=start + dt.timedelta(days=index),
            engagement_rate=mean + (spread if index % 2 else -spread),
            campaign_id=None,
            dimensions={"format": "FEED"},
        )
        for index in range(n)
    ]


class TestGrading:
    """P7-06. Three grades, and the bar for each is a published number."""

    def test_under_eight_is_insufficient(self) -> None:
        assert statistics.grade(7, campaigns_observed=3, clears_noise_band=True) == (
            Confidence.INSUFFICIENT
        )

    def test_eight_is_emerging(self) -> None:
        assert statistics.grade(8, campaigns_observed=3, clears_noise_band=True) == (
            Confidence.EMERGING
        )

    def test_twenty_with_two_campaigns_clearing_the_band_is_strong(self) -> None:
        assert statistics.grade(20, campaigns_observed=2, clears_noise_band=True) == (
            Confidence.STRONG
        )

    def test_nineteen_is_not_strong(self) -> None:
        assert statistics.grade(19, campaigns_observed=5, clears_noise_band=True) == (
            Confidence.EMERGING
        )

    def test_one_campaign_is_not_strong_however_large(self) -> None:
        """An effect seen in a single campaign is a fact about that campaign.

        Strong is what may produce a *rule*, and a rule derived from one
        campaign encodes that campaign's season, offer and audience as if they
        were the brand's permanent physics."""
        assert statistics.grade(500, campaigns_observed=1, clears_noise_band=True) == (
            Confidence.EMERGING
        )

    def test_an_effect_inside_the_noise_band_is_never_strong(self) -> None:
        assert statistics.grade(500, campaigns_observed=9, clears_noise_band=False) == (
            Confidence.EMERGING
        )


class TestAnalyse:
    def test_a_segment_is_compared_against_everything_else_in_the_window(self) -> None:
        observations = rows(n=20, mean=0.08) + baseline(n=40, mean=0.04)

        found = {(stat.dimension, stat.value): stat for stat in statistics.analyse(observations)}
        carousel = found[("format", "CAROUSEL")]

        assert carousel.sample_size == 20
        assert carousel.baseline_size == 40
        assert carousel.segment_mean == pytest.approx(0.08)
        assert carousel.baseline_mean == pytest.approx(0.04)

    def test_every_stat_carries_a_sample_size(self) -> None:
        """P7-07. Not a presentation concern — a stat with no `n` is not a stat."""
        for stat in statistics.analyse(rows(n=12, mean=0.05) + baseline(n=12, mean=0.05)):
            assert stat.sample_size > 0

    def test_led_in_counts_posts_not_percentages(self) -> None:
        """The digest may say "7 of your last 9", so the count is the primitive.

        A ratio computed here and multiplied back out downstream is how a
        rounded percentage becomes a claim about posts that do not exist."""
        observations = [
            *rows(n=3, mean=0.09),
            *baseline(n=10, mean=0.04),
        ]
        carousel = next(
            stat for stat in statistics.analyse(observations) if stat.value == "CAROUSEL"
        )
        assert carousel.led_in == 3
        assert carousel.sample_size == 3

    def test_a_segment_of_one_is_still_reported_as_insufficient(self) -> None:
        """Dropped silently, it would look like the format was never tried."""
        observations = rows(n=1, mean=0.9) + baseline(n=30, mean=0.04)
        single = next(stat for stat in statistics.analyse(observations) if stat.value == "CAROUSEL")
        assert single.confidence == Confidence.INSUFFICIENT
        assert single.sample_size == 1

    def test_a_segment_with_no_baseline_to_compare_against_is_excluded(self) -> None:
        """Nothing else in the window means there is no comparison to make.

        Reported with a reason rather than dropped, because "we could not
        compare this" and "this did not happen" are different facts and the
        digest has to be able to say which (C-07 applied to segments)."""
        only = statistics.analyse(rows(n=9, mean=0.05))

        carousel = next(stat for stat in only if stat.value == "CAROUSEL")
        assert carousel.excluded_reason
        assert carousel.confidence == Confidence.INSUFFICIENT

    def test_an_unavailable_metric_never_counts_as_a_zero(self) -> None:
        """C-07, applied where it does the most damage.

        A null engagement rate averaged as 0.0 drags a segment's mean down in
        proportion to how *unmeasurable* it was, which reliably makes the
        best-covered platform look like the best-performing one."""
        observations = [
            *rows(n=4, mean=0.08),
            statistics.Observation(
                target_id=99,
                post_id=99,
                published_at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                engagement_rate=None,
                campaign_id=1,
                dimensions={"format": "CAROUSEL"},
            ),
            *baseline(n=10, mean=0.04),
        ]

        carousel = next(
            stat for stat in statistics.analyse(observations) if stat.value == "CAROUSEL"
        )
        assert carousel.sample_size == 4, "the unmeasured post must not enter the denominator"
        assert carousel.segment_mean == pytest.approx(0.08)

    def test_campaigns_observed_counts_distinct_campaigns(self) -> None:
        observations = rows(n=20, mean=0.08, campaigns=3) + baseline(n=20, mean=0.04)
        carousel = next(
            stat for stat in statistics.analyse(observations) if stat.value == "CAROUSEL"
        )
        assert carousel.campaigns_observed == 3

    def test_a_flat_baseline_still_admits_a_real_effect(self) -> None:
        """A zero-spread baseline must not make the noise band zero-width.

        Otherwise the first two posts of a workspace's life clear it by
        definition and everything is Strong on day one — the exact failure
        P7-05 exists to prevent, arriving through the other door."""
        observations = rows(n=20, mean=0.0401, campaigns=4) + baseline(n=40, mean=0.04, spread=0.0)
        carousel = next(
            stat for stat in statistics.analyse(observations) if stat.value == "CAROUSEL"
        )
        assert carousel.confidence != Confidence.STRONG
