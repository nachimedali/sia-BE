"""Aggregation (P8-03, P8-04) — distributions, and the thresholds that guard them.

The aggregator reads the projection and nothing else. Every test here writes
projection rows directly for that reason: there is no other input to arrange.
"""

from __future__ import annotations

import statistics
from typing import Any

import pytest

from benchmarks.models import BenchmarkConfig, BenchmarkRun, CohortBenchmark
from benchmarks.services import aggregate
from benchmarks.tests.fixtures import NOW
from common.records import AppendOnlyError

pytestmark = pytest.mark.django_db


def only_cohort(run: BenchmarkRun | None) -> CohortBenchmark:
    assert run is not None
    return run.cohorts.get()


class TestThresholds:
    """`N ≥ 8` workspaces **and** `M ≥ 200` posts. Below either, no number."""

    def test_eight_workspaces_and_two_hundred_posts_is_a_benchmark(
        self, observe: Any, vertical: Any
    ) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25)

        cohort = only_cohort(aggregate.compute(now=NOW))

        assert cohort.sufficient is True
        assert cohort.contributors == 8
        assert cohort.posts == 200
        assert set(cohort.metrics) == {"engagement_rate", "reach_rate", "comment_rate"}

    def test_seven_workspaces_is_not_however_many_posts(self, observe: Any, vertical: Any) -> None:
        observe(vertical=vertical, contributors=7, posts_each=40)

        cohort = only_cohort(aggregate.compute(now=NOW))

        assert cohort.sufficient is False
        assert cohort.metrics == {}
        assert cohort.best_windows == []

    def test_one_hundred_and_ninety_nine_posts_is_not(self, observe: Any, vertical: Any) -> None:
        observe(vertical=vertical, contributors=8, posts_each=24)
        observe(vertical=vertical, contributors=7, posts_each=1, contributor_prefix="extra")

        cohort = only_cohort(aggregate.compute(now=NOW))

        assert cohort.posts == 199
        assert cohort.sufficient is False
        assert cohort.metrics == {}

    def test_thresholds_are_read_from_config_and_recorded_on_the_run(
        self, observe: Any, vertical: Any
    ) -> None:
        """Admin-editable, and a later edit does not reinterpret an old run."""
        config = BenchmarkConfig.get_solo()
        config.min_workspaces = 4
        config.min_posts = 40
        config.save()
        observe(vertical=vertical, contributors=4, posts_each=10)

        run = aggregate.compute(now=NOW)

        assert run is not None
        assert (run.min_workspaces, run.min_posts) == (4, 40)
        assert only_cohort(run).sufficient is True

    def test_a_metric_short_of_the_threshold_is_withheld_on_its_own(
        self, observe: Any, vertical: Any
    ) -> None:
        """A cohort on a platform that does not report impressions still has an
        engagement benchmark; its reach benchmark is withheld, not zero."""
        observe(vertical=vertical, contributors=8, posts_each=25, reach_rate=None)

        cohort = only_cohort(aggregate.compute(now=NOW))

        assert cohort.sufficient is True
        assert "engagement_rate" in cohort.metrics
        assert "reach_rate" not in cohort.metrics
        assert cohort.metric_counts["reach_rate"] == {"contributors": 0, "posts": 0}


class TestDistributions:
    def test_median_and_quartiles_are_computed_in_code(self, observe: Any, vertical: Any) -> None:
        def rate(contributor: int, post: int) -> float:
            return round(0.01 + contributor * 0.002 + post * 0.0001, 6)

        observe(vertical=vertical, contributors=8, posts_each=25, engagement_rate=rate)
        values = [rate(c, p) for c in range(8) for p in range(25)]
        p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")

        stats = only_cohort(aggregate.compute(now=NOW)).metrics["engagement_rate"]

        assert stats["median"] == pytest.approx(statistics.median(values))
        assert stats["p25"] == pytest.approx(p25)
        assert stats["p75"] == pytest.approx(p75)
        assert stats["posts"] == 200

    def test_one_prolific_workspace_cannot_become_the_benchmark(
        self, observe: Any, vertical: Any
    ) -> None:
        """Eight brands, one of which posts 400 times. Uncapped, the median is
        that one brand's median wearing a cohort's name — a benchmark in form
        and a single competitor's numbers in substance."""
        observe(vertical=vertical, contributors=7, posts_each=25, engagement_rate=0.01)
        observe(
            vertical=vertical,
            contributors=1,
            posts_each=400,
            engagement_rate=0.09,
            contributor_prefix="whale",
        )
        cap = BenchmarkConfig.get_solo().max_posts_per_contributor

        cohort = only_cohort(aggregate.compute(now=NOW))

        assert cohort.posts == 7 * 25 + cap
        assert cohort.metrics["engagement_rate"]["median"] == pytest.approx(0.01)

    def test_cohorts_are_separated_by_every_part_of_the_key(
        self, observe: Any, vertical: Any
    ) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25, size_band="0_1000")
        observe(vertical=vertical, contributors=8, posts_each=25, size_band="10000_100000")
        observe(vertical=vertical, contributors=8, posts_each=25, market="FR")
        observe(vertical=vertical, contributors=8, posts_each=25, post_format="REEL")

        run = aggregate.compute(now=NOW)

        assert run is not None
        assert run.cohorts.count() == 4
        assert all(cohort.posts == 200 for cohort in run.cohorts.all())


class TestBestWindows:
    def test_windows_are_ranked_and_a_thin_window_is_withheld(
        self, observe: Any, vertical: Any
    ) -> None:
        observe(
            vertical=vertical,
            contributors=8,
            posts_each=15,
            engagement_rate=0.08,
            posting_window="evening",
        )
        observe(
            vertical=vertical,
            contributors=8,
            posts_each=10,
            engagement_rate=0.03,
            posting_window="morning",
            contributor_prefix="c",
        )
        # Three workspaces is below N: this window may not be reported at all.
        observe(
            vertical=vertical,
            contributors=3,
            posts_each=5,
            engagement_rate=0.5,
            posting_window="night",
            contributor_prefix="c",
        )

        windows = only_cohort(aggregate.compute(now=NOW)).best_windows

        assert [window["window"] for window in windows] == ["evening", "morning"]
        assert windows[0]["median_engagement_rate"] == pytest.approx(0.08)


class TestRuns:
    def test_an_empty_projection_writes_no_run(self, db: None) -> None:
        assert aggregate.compute(now=NOW) is None
        assert BenchmarkRun.objects.count() == 0

    def test_each_run_is_a_new_document(self, observe: Any, vertical: Any) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25)

        first = aggregate.compute(now=NOW)
        second = aggregate.compute(now=NOW)

        assert first is not None and second is not None
        assert first.pk != second.pk
        assert CohortBenchmark.objects.count() == 2

    def test_a_published_benchmark_cannot_be_edited(self, observe: Any, vertical: Any) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25)
        cohort = only_cohort(aggregate.compute(now=NOW))

        cohort.posts = 1
        with pytest.raises(AppendOnlyError):
            cohort.save()

    def test_rows_outside_the_window_are_not_counted(self, observe: Any, vertical: Any) -> None:
        observe(vertical=vertical, contributors=8, posts_each=25)
        observe(vertical=vertical, contributors=8, posts_each=25, days_ago=400, market="ES")

        run = aggregate.compute(now=NOW)

        assert run is not None
        assert list(run.cohorts.values_list("market", flat=True)) == ["PT"]
