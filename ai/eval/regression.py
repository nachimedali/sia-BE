"""Comparing an eval run to the committed baseline (implementation.md Phase 7.8).

A regression is a drop of more than `tolerance` on the two headline numbers
(`pass_rate`, `mean_identity_score`) or any single case flipping from passed
to failed — an aggregate can hold steady while one specific case quietly
breaks, and that is exactly the kind of thing this harness exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).with_name("baseline.json")


def find_regressions(
    current: dict[str, Any], baseline: dict[str, Any], *, tolerance: float = 0.05
) -> list[str]:
    regressions: list[str] = []

    for metric in ("pass_rate", "mean_identity_score"):
        current_value = current.get(metric)
        baseline_value = baseline.get(metric)
        if current_value is None or baseline_value is None:
            continue
        if current_value < baseline_value - tolerance:
            regressions.append(
                f"{metric} dropped: {baseline_value:.4f} -> {current_value:.4f} "
                f"(tolerance {tolerance})"
            )

    baseline_cases: dict[str, Any] = baseline.get("cases", {})
    current_cases: dict[str, Any] = current.get("cases", {})
    for name, baseline_case in baseline_cases.items():
        current_case = current_cases.get(name)
        if current_case is None:
            regressions.append(f"{name}: present in baseline but missing from this run")
            continue
        if baseline_case["passed"] and not current_case["passed"]:
            regressions.append(f"{name}: passed in baseline, now fails")

    return regressions
