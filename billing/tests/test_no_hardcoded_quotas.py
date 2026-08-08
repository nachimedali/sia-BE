"""I8 — every plan quota is a DB row, never a code constant.

The invariant exists so an operator can retune a plan in admin without a
deploy. It fails quietly: a hardcoded `150` keeps working, keeps passing tests,
and simply ignores the admin edit — so the only reliable guard is a test that
reads the source.

Two checks, because the two ways this gets violated look different:

1. a **price or allowance literal** appearing anywhere in application code;
2. a **quota field name next to a number**, which is what `if
   workspace.plan.max_products >= 10` looks like.

Small values (1, 5, 7) are deliberately not scanned on their own: they collide
with every loop bound and HTTP status in the codebase, and a check that cries
wolf gets deleted. Check 2 catches them in the shape that actually matters.
"""

from __future__ import annotations

import re
from pathlib import Path

from billing.models import QUOTA_FIELDS

APP_ROOT = Path(__file__).resolve().parent.parent.parent

# Where quota values are *supposed* to live.
EXEMPT = (
    "/migrations/",
    "/tests/",
    "/management/commands/seed_",
    "/.venv/",
    "/conftest.py",
    # Declares the columns, so it necessarily carries their schema defaults.
    "/billing/models.py",
)

# From the §4.1 matrix. Restricted to values distinctive enough that a match is
# evidence rather than noise — 400 and 500 are HTTP statuses, 30 is a timeout.
PRICE_AND_ALLOWANCE_LITERALS = (3700, 34800, 9700, 92400, 5000, 34_800)

QUOTA_NEAR_NUMBER = re.compile(
    r"\b(" + "|".join(sorted(QUOTA_FIELDS)) + r")\b\s*(?:[=<>!]=|[<>]|=)\s*-?\d+"
)


def _application_sources() -> list[Path]:
    return [
        path for path in APP_ROOT.rglob("*.py") if not any(marker in str(path) for marker in EXEMPT)
    ]


def test_no_hardcoded_quota_literals_in_app_code() -> None:
    """I8."""
    offenders: list[str] = []

    for path in _application_sources():
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), start=1):
            code = line.split("#", 1)[0]

            for literal in PRICE_AND_ALLOWANCE_LITERALS:
                if re.search(rf"\b{literal}\b", code):
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{lineno}: {line.strip()}")

            match = QUOTA_NEAR_NUMBER.search(code)
            if match:
                offenders.append(f"{path.relative_to(APP_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Plan quotas and prices must be read from the Plan row, never written as "
        "literals (I8). Offending lines:\n  " + "\n  ".join(offenders)
    )


def test_the_check_would_actually_catch_a_violation(tmp_path) -> None:
    """A guard that cannot fail is not a guard.

    Without this, deleting the literal list or breaking the regex would leave a
    permanently green test asserting nothing.
    """
    violation = "if workspace.plan.max_products >= 10:\n"
    assert QUOTA_NEAR_NUMBER.search(violation)

    assert re.search(r"\b3700\b", "price_monthly_cents = 3700")
    assert not QUOTA_NEAR_NUMBER.search("max_products = plan.max_products\n")
