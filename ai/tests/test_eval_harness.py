"""The evaluation harness runs in the normal suite, not only as a script
someone remembers to invoke (design.md §8.3: "a regression test, not
polish")."""

from __future__ import annotations

import json

import pytest

from ai.eval.harness import run_benchmark
from ai.eval.regression import BASELINE_PATH, find_regressions

pytestmark = pytest.mark.django_db


def test_baseline_file_is_committed_and_well_formed() -> None:
    baseline = json.loads(BASELINE_PATH.read_text())
    assert 0.0 <= baseline["pass_rate"] <= 1.0
    assert baseline["cases"]

    from ai.eval.harness import BENCHMARK_CASES

    assert set(baseline["cases"]) == {case.name for case in BENCHMARK_CASES}


def test_current_run_does_not_regress_against_the_committed_baseline(
    settings: object,
) -> None:
    from django.core.management import call_command

    call_command("seed_plans", verbosity=0)
    call_command("seed_generation_costs", verbosity=0)

    result = run_benchmark()
    baseline = json.loads(BASELINE_PATH.read_text())

    regressions = find_regressions(result.as_dict(), baseline, tolerance=0.05)
    assert regressions == []


def test_find_regressions_flags_a_pass_rate_drop() -> None:
    baseline = {"pass_rate": 0.9, "mean_identity_score": 0.8, "cases": {}}
    current = {"pass_rate": 0.5, "mean_identity_score": 0.8, "cases": {}}

    regressions = find_regressions(current, baseline, tolerance=0.05)

    assert any("pass_rate" in r for r in regressions)


def test_find_regressions_flags_a_case_that_now_fails() -> None:
    baseline = {
        "pass_rate": 1.0,
        "mean_identity_score": None,
        "cases": {"a": {"passed": True}},
    }
    current = {
        "pass_rate": 1.0,
        "mean_identity_score": None,
        "cases": {"a": {"passed": False}},
    }

    regressions = find_regressions(current, baseline, tolerance=0.05)

    assert any("a: passed in baseline" in r for r in regressions)


def test_find_regressions_tolerates_small_noise() -> None:
    baseline = {"pass_rate": 0.9, "mean_identity_score": 0.8, "cases": {}}
    current = {"pass_rate": 0.87, "mean_identity_score": 0.79, "cases": {}}

    assert find_regressions(current, baseline, tolerance=0.05) == []
