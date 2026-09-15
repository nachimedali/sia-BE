"""The nightly benchmark refresh, on `analyze_q`.

One task rather than two so the order cannot drift: aggregation reads the
projection, and a projection refreshed after the aggregate would publish last
night's cohort under tonight's date. Retries land on the next schedule, as for
Learn — the inputs are a trailing window and nothing is lost by waiting.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from benchmarks.services import aggregate, projection

logger = logging.getLogger(__name__)


@shared_task(name="benchmarks.tasks.refresh_benchmarks")
def refresh_benchmarks() -> int | None:
    """Project every contributor, then aggregate. Returns the run id, or None."""
    moment = timezone.now()
    projected = projection.project_all(now=moment)
    run = aggregate.compute(now=moment)
    logger.info(
        "benchmarks refreshed",
        extra={"observations": projected, "run_id": run.pk if run else None},
    )
    return run.pk if run else None
