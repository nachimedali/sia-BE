"""Runs the nightly benchmark refresh once, on demand.

The same gap `publish_due_posts` fills: nothing plays Celery Beat's role under
Playwright's webServer, and an operator who has just published new consent
terms or edited a threshold should not wait until 04:40 to see the effect. The
refresh is idempotent in effect — the projection is rebuilt, and aggregation
writes a new run rather than editing the last one.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from benchmarks.tasks import refresh_benchmarks


class Command(BaseCommand):
    help = "Rebuilds the benchmark projection and computes a new benchmark run."

    def handle(self, *args: Any, **options: Any) -> None:
        run_id = refresh_benchmarks()
        if run_id is None:
            self.stdout.write("Nothing projected; no run written.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Benchmark run {run_id} written."))
