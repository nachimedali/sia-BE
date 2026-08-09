"""`manage.py run_eval_harness` (implementation.md Phase 7.8)."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from ai.eval.regression import BASELINE_PATH

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(generation_costs: Any) -> None:
    call_command("seed_plans", verbosity=0)


def test_reports_no_regressions_against_the_committed_baseline(seeded: Any) -> None:
    out = StringIO()
    call_command("run_eval_harness", stdout=out)

    printed = out.getvalue()
    assert "No regressions." in printed
    payload = json.loads(printed.split("No regressions.")[0])
    assert payload["pass_rate"] == 1.0


def test_update_baseline_writes_the_file(seeded: Any, tmp_path: Any, monkeypatch: Any) -> None:
    scratch = tmp_path / "baseline.json"
    scratch.write_text(BASELINE_PATH.read_text())
    monkeypatch.setattr("ai.management.commands.run_eval_harness.BASELINE_PATH", scratch)

    out = StringIO()
    call_command("run_eval_harness", "--update-baseline", stdout=out)

    assert "Baseline written" in out.getvalue()
    assert json.loads(scratch.read_text())["pass_rate"] == 1.0


def test_exits_nonzero_on_a_regression(seeded: Any, tmp_path: Any, monkeypatch: Any) -> None:
    regressed = tmp_path / "baseline.json"
    regressed.write_text(json.dumps({"pass_rate": 1.0, "mean_identity_score": None, "cases": {}}))
    monkeypatch.setattr("ai.management.commands.run_eval_harness.BASELINE_PATH", regressed)

    def _always_fails() -> Any:
        from ai.eval.harness import EvalCaseResult, EvalResult

        return EvalResult(
            cases=[
                EvalCaseResult(
                    name="x", passed=False, attempts=3, identity_score=None, credits_charged=0
                )
            ]
        )

    monkeypatch.setattr("ai.management.commands.run_eval_harness.run_benchmark", _always_fails)

    with pytest.raises(SystemExit) as excinfo:
        call_command("run_eval_harness", stderr=StringIO(), stdout=StringIO())

    assert excinfo.value.code == 1
