"""What a workspace reads (P8-04, P8-05).

Two rules shape every response here:

* **A sub-threshold cohort carries a shortfall and nothing else** (P8-G1). Not
  a null median, not a zero — the keys are absent — so no client can render a
  number that was never computed.
* **A workspace sees only the cohorts its own posts are in.** The run holds
  cohorts for every vertical and market the product has contributors in;
  listing them would tell one customer where the others are.

The shortfall is measured against the thresholds the run was computed under,
which are copied onto the run for this reason: today's thresholds applied to
last night's counts would state a gap that was never true.
"""

from __future__ import annotations

from typing import Any

from benchmarks.markets import all_markets
from benchmarks.models import BenchmarkObservation, BenchmarkRun, CohortBenchmark, ConsentRecord
from benchmarks.services import consent
from benchmarks.services.aggregate import METRICS
from benchmarks.services.contributors import contributor_token
from benchmarks.services.projection import vertical_of
from workspaces.models import Workspace

OK = "ok"
INSUFFICIENT = "insufficient_cohort_data"
PENDING = "pending"

#: Enough history to answer "when did we opt in, and did anyone withdraw",
#: without turning the participation screen into an audit log.
HISTORY_LIMIT = 20


def _shortfall(run: BenchmarkRun, *, contributors: int, posts: int) -> dict[str, int]:
    return {
        "workspaces": max(0, run.min_workspaces - contributors),
        "posts": max(0, run.min_posts - posts),
    }


def _entry(
    key: dict[str, str], run: BenchmarkRun | None, cohort: CohortBenchmark | None
) -> dict[str, Any]:
    if run is None or cohort is None:
        return {**key, "status": PENDING}

    if not cohort.sufficient:
        return {
            **key,
            "status": INSUFFICIENT,
            "shortfall": _shortfall(run, contributors=cohort.contributors, posts=cohort.posts),
        }

    metrics: dict[str, Any] = {}
    for metric in METRICS:
        if metric in cohort.metrics:
            metrics[metric] = {"status": OK, **cohort.metrics[metric]}
        elif metric in cohort.metric_counts:
            counts = cohort.metric_counts[metric]
            metrics[metric] = {
                "status": INSUFFICIENT,
                "shortfall": _shortfall(
                    run, contributors=counts["contributors"], posts=counts["posts"]
                ),
            }
    return {
        **key,
        "status": OK,
        "contributors": cohort.contributors,
        "posts": cohort.posts,
        "metrics": metrics,
        "best_windows": cohort.best_windows,
    }


def benchmarks_for(workspace: Workspace) -> dict[str, Any]:
    if not consent.is_contributing(workspace):
        raise consent.BenchmarkConsentRequired()

    own = list(
        BenchmarkObservation.objects.filter(contributor=contributor_token(workspace.pk))
        .values_list("vertical_id", "market", "platform", "size_band", "post_format")
        .distinct()
        .order_by("platform", "post_format", "size_band", "vertical_id", "market")
    )
    run = BenchmarkRun.objects.order_by("-computed_at", "-id").first()

    published: dict[tuple[Any, ...], CohortBenchmark] = {}
    if run is not None and own:
        for cohort in run.cohorts.filter(
            vertical_id__in={row[0] for row in own}, market__in={row[1] for row in own}
        ):
            published[
                (
                    cohort.vertical_id,
                    cohort.market,
                    cohort.platform,
                    cohort.size_band,
                    cohort.post_format,
                )
            ] = cohort

    return {
        "run": None
        if run is None
        else {
            "computed_at": run.computed_at,
            "window_start": run.window_start,
            "window_end": run.window_end,
        },
        "cohorts": [
            _entry(
                {"platform": row[2], "size_band": row[3], "post_format": row[4]},
                run,
                published.get(row),
            )
            for row in own
        ],
    }


def participation(workspace: Workspace) -> dict[str, Any]:
    current = consent.status(workspace)
    vertical = None
    if workspace.category is not None:
        root = vertical_of(workspace.category)
        vertical = {"id": root.pk, "name": root.name}

    history = (
        ConsentRecord.objects.filter(workspace=workspace)
        .select_related("policy")
        .order_by("-recorded_at", "-id")[:HISTORY_LIMIT]
    )
    return {
        "contributing": current.contributing,
        "requires_reconsent": current.requires_reconsent,
        "policy": None
        if current.policy is None
        else {
            "version": current.policy.version,
            "summary": current.policy.summary,
            "document_url": current.policy.document_url,
            "published_at": current.policy.published_at,
        },
        "consent": {
            "policy_version": current.latest.policy.version,
            "granted_at": current.latest.recorded_at,
        }
        if current.granted and current.latest is not None
        else None,
        "market": workspace.market,
        "vertical": vertical,
        "markets": [{"code": code, "name": name} for code, name in all_markets().items()],
        "history": [
            {
                "action": record.action,
                "policy_version": record.policy.version,
                "recorded_at": record.recorded_at,
            }
            for record in history
        ],
    }
