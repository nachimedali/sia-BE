"""The latest finished run, put on a post queryset as one JSON column — how
the review queue badges a page of posts in the one query that lists them."""

from __future__ import annotations

from typing import Any

from django.db.models import OuterRef, QuerySet, Subquery
from django.db.models.functions import JSONObject

from checks.models import CheckRun, CheckRunState


def annotate(queryset: QuerySet[Any]) -> QuerySet[Any]:
    """`checks_latest`: `{verdict, counts, fingerprint, run_at}`, or `None` for
    a post never checked. One correlated lookup per row, not one per field."""
    latest = CheckRun.objects.filter(post=OuterRef("pk"), state=CheckRunState.DONE).order_by(
        "-created_at", "-id"
    )
    row = JSONObject(
        verdict="verdict", counts="counts", fingerprint="fingerprint", run_at="finished_at"
    )
    annotated: QuerySet[Any] = queryset.annotate(
        checks_latest=Subquery(latest.values_list(row)[:1])
    )
    return annotated
