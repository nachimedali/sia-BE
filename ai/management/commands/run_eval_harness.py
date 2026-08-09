"""Runs the evaluation harness (implementation.md Phase 7.8).

    python manage.py run_eval_harness                  # compare to the baseline
    python manage.py run_eval_harness --update-baseline # write a new one

Exits non-zero on a regression so this is usable as a CI gate independent of
`ai/tests/test_eval_harness.py`, which asserts the same thing inside the
normal test run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ai.eval.harness import run_benchmark
from ai.eval.regression import BASELINE_PATH, find_regressions

TOLERANCE = 0.05


class Command(BaseCommand):
    help = "Run the generation quality benchmark and compare it to the committed baseline."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--update-baseline", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        result = run_benchmark()
        payload = result.as_dict()
        self.stdout.write(json.dumps(payload, indent=2))

        if options["update_baseline"]:
            Path(BASELINE_PATH).write_text(json.dumps(payload, indent=2) + "\n")
            self.stdout.write(self.style.SUCCESS(f"Baseline written to {BASELINE_PATH}."))
            return

        baseline = json.loads(Path(BASELINE_PATH).read_text())
        regressions = find_regressions(payload, baseline, tolerance=TOLERANCE)
        if regressions:
            for line in regressions:
                self.stderr.write(self.style.ERROR(line))
            self.stderr.write(self.style.ERROR(f"{len(regressions)} regression(s) found."))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("No regressions."))
