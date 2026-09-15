"""Aggregation (P8-03, P8-04) — cohort distributions from the projection alone.

**This module may import `benchmarks.models` and nothing from any tenant app.**
That is P8-08's hard stop in structural form: if the aggregation job could read
raw content at all, the projection would be wrong, so the job is given nothing
it could read it with. `tests/test_phase8_gates.py` parses this file's imports
and captures the SQL it issues; both fail on the first line that reaches past
the projection.

Three rules decide what a cohort may report:

* **`N` workspaces and `M` posts, or no number.** Checked for the cohort as a
  whole, and again for each metric on its own non-null values — a platform that
  does not report impressions still has an engagement benchmark, and its reach
  benchmark is withheld rather than computed from the few that do.
* **One brand's share is capped**, most recent posts first. Otherwise eight
  brands where one posts four hundred times publish that one brand's median
  under a cohort's name.
* **A posting window is reported only if it too spans `N` workspaces.** "Your
  cohort does best in the evening" drawn from three brands is three brands.
"""

from __future__ import annotations

import datetime as dt
import itertools
import statistics
from typing import Any

from django.db import transaction
from django.utils import timezone

from benchmarks.models import BenchmarkConfig, BenchmarkObservation, BenchmarkRun, CohortBenchmark

METRICS: tuple[str, ...] = ("engagement_rate", "reach_rate", "comment_rate")

_KEY = ("vertical_id", "market", "platform", "size_band", "post_format")
_COLUMNS = (*_KEY, "contributor", "posting_window", *METRICS)
_CONTRIBUTOR = len(_KEY)
_WINDOW = _CONTRIBUTOR + 1
_FIRST_METRIC = _WINDOW + 1


def _distribution(values: list[float]) -> dict[str, Any]:
    p25, median, p75 = statistics.quantiles(values, n=4, method="inclusive")
    return {
        "median": round(median, 6),
        "p25": round(p25, 6),
        "p75": round(p75, 6),
        "posts": len(values),
    }


def _cap(rows: list[tuple[Any, ...]], limit: int) -> list[tuple[Any, ...]]:
    """At most `limit` rows per contributor. Rows arrive newest first within
    each contributor, so the cap keeps the most recent posts."""
    kept: list[tuple[Any, ...]] = []
    for _, group in itertools.groupby(rows, key=lambda row: row[_CONTRIBUTOR]):
        kept.extend(itertools.islice(group, limit))
    return kept


def _cohort(
    run: BenchmarkRun, key: tuple[Any, ...], rows: list[tuple[Any, ...]]
) -> CohortBenchmark:
    rows = _cap(rows, run.max_posts_per_contributor)
    contributors = len({row[_CONTRIBUTOR] for row in rows})
    sufficient = contributors >= run.min_workspaces and len(rows) >= run.min_posts

    metrics: dict[str, Any] = {}
    counts: dict[str, dict[str, int]] = {}
    for offset, metric in enumerate(METRICS):
        present = [row for row in rows if row[_FIRST_METRIC + offset] is not None]
        metric_contributors = len({row[_CONTRIBUTOR] for row in present})
        counts[metric] = {"contributors": metric_contributors, "posts": len(present)}
        if (
            sufficient
            and metric_contributors >= run.min_workspaces
            and len(present) >= run.min_posts
        ):
            metrics[metric] = _distribution([row[_FIRST_METRIC + offset] for row in present])

    windows: list[dict[str, Any]] = []
    if sufficient:
        engagement = _FIRST_METRIC + METRICS.index("engagement_rate")
        by_window: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            if row[engagement] is not None:
                by_window.setdefault(row[_WINDOW], []).append(row)
        for window, members in by_window.items():
            if len({row[_CONTRIBUTOR] for row in members}) < run.min_workspaces:
                continue
            windows.append(
                {
                    "window": window,
                    "median_engagement_rate": round(
                        statistics.median(row[engagement] for row in members), 6
                    ),
                    "posts": len(members),
                }
            )
        windows.sort(key=lambda item: (-item["median_engagement_rate"], item["window"]))

    vertical_id, market, platform, band, post_format = key
    return CohortBenchmark(
        run=run,
        vertical_id=vertical_id,
        market=market,
        platform=platform,
        size_band=band,
        post_format=post_format,
        contributors=contributors,
        posts=len(rows),
        sufficient=sufficient,
        metrics=metrics,
        metric_counts=counts,
        best_windows=windows,
    )


def compute(*, now: dt.datetime | None = None) -> BenchmarkRun | None:
    """One run over the trailing window. `None` when nothing was projected.

    Writing an empty run on a night nobody contributes would put a document in
    the history that says nothing and looks like a result.
    """
    moment = now or timezone.now()
    config = BenchmarkConfig.get_solo()
    window_end = moment.date()
    window_start = window_end - dt.timedelta(days=config.window_days)

    scoped = BenchmarkObservation.objects.filter(
        published_on__gte=window_start, published_on__lte=window_end
    )
    if not scoped.exists():
        return None

    ordered = scoped.order_by(*_KEY, "contributor", "-published_on", "-id").values_list(*_COLUMNS)

    with transaction.atomic():
        run = BenchmarkRun.objects.create(
            window_start=window_start,
            window_end=window_end,
            min_workspaces=config.min_workspaces,
            min_posts=config.min_posts,
            max_posts_per_contributor=config.max_posts_per_contributor,
        )
        cohorts = [
            _cohort(run, key, list(rows))
            for key, rows in itertools.groupby(
                ordered.iterator(chunk_size=2000), key=lambda row: row[:_CONTRIBUTOR]
            )
        ]
        CohortBenchmark.objects.bulk_create(cohorts)
    return run
