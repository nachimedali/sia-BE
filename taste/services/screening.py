"""Hard-constraint screening — brand policy (C-09, P5-02).

**A different layer from the quality gate, and the separation is the point.**
`ai/services/quality.py` judges the *artifact*: resolution, aspect, whether the
product is actually in the picture, whether the body is blank. This judges the
*brand*: never mention a competitor, never show alcohol, always include a call
to action. A candidate can be a technically flawless image that violates policy,
and merging the two checks is the failure C-09 names explicitly.

**Evaluated in code, never asked of the model.** A policy the generator
self-assesses is a policy that fails precisely when the prompt has drifted —
the moment it matters most. Every check below is deterministic and readable.

Declared as data like `rules.py`: a constraint kind is a row, so a workspace's
policy vocabulary grows by an entry plus a test rather than a branch.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rest_framework.exceptions import ValidationError

from common.text import HASHTAG_RE

#: Words that make a sentence an instruction. Deliberately small and boring:
#: a cleverer detector would be one nobody can predict, and a CTA check that
#: surprises people gets switched off.
_CTA_VERBS = (
    "shop",
    "buy",
    "book",
    "order",
    "get",
    "try",
    "join",
    "read",
    "learn",
    "discover",
    "explore",
    "sign up",
    "visit",
    "call",
    "download",
)
_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class ScreenResult:
    passed: bool
    #: Every violation, not merely the first — a composer that fixed one
    #: problem only to meet the next would make screening feel like a fight.
    violations: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.violations)


def _banned_phrases(body: str, value: Any) -> list[str]:
    """Word-boundary matches, not substrings.

    A brand banning "Ace" should not have every "place" and "surface" refused.
    A screening layer that cries wolf gets switched off, which is strictly
    worse than not having one.
    """
    hits = [
        phrase
        for phrase in value
        if re.search(rf"\b{re.escape(str(phrase))}\b", body, re.IGNORECASE)
    ]
    return [f"banned_phrases: contains {hits}"] if hits else []


def _banned_topics(body: str, value: Any) -> list[str]:
    hits = [
        topic for topic in value if re.search(rf"\b{re.escape(str(topic))}\b", body, re.IGNORECASE)
    ]
    return [f"banned_topics: mentions {hits}"] if hits else []


def _required_phrases(body: str, value: Any) -> list[str]:
    missing = [phrase for phrase in value if str(phrase).lower() not in body.lower()]
    return [f"required_phrases: missing {missing}"] if missing else []


def _require_cta(body: str, value: Any) -> list[str]:
    if not value:
        return []
    lowered = body.lower()
    has_cta = bool(_LINK_RE.search(body)) or any(verb in lowered for verb in _CTA_VERBS)
    return [] if has_cta else ["require_cta: no call to action found"]


def _max_hashtags(body: str, value: Any) -> list[str]:
    count = len(HASHTAG_RE.findall(body))
    return [f"max_hashtags: {count} used, {value} allowed"] if count > int(value) else []


#: Constraint kind → the check, and the shape its value must take. Declared
#: rather than branched, so a new policy is an entry plus a test.
CONSTRAINT_KINDS: dict[str, Callable[[str, Any], list[str]]] = {
    "banned_phrases": _banned_phrases,
    "banned_topics": _banned_topics,
    "required_phrases": _required_phrases,
    "require_cta": _require_cta,
    "max_hashtags": _max_hashtags,
}

#: What each kind's value must be. A constraint stored in the wrong shape would
#: fail at screening time — on the one path where failing means a candidate is
#: silently discarded — so it is refused at the write instead.
_CONSTRAINT_SHAPES: dict[str, type] = {
    "banned_phrases": list,
    "banned_topics": list,
    "required_phrases": list,
    "require_cta": bool,
    "max_hashtags": int,
}


def validate_constraints(constraints: Any) -> dict[str, Any]:
    """Refused, never ignored.

    A constraint kind nothing evaluates is a policy the workspace believes it
    has — which is worse than no policy at all, because nobody goes looking
    for it.
    """
    if not isinstance(constraints, dict):
        raise ValidationError({"hard_constraints": "Constraints are an object of kind → value."})

    unknown = sorted(set(constraints) - set(CONSTRAINT_KINDS))
    if unknown:
        raise ValidationError(
            {
                "hard_constraints": f"Unknown constraint(s): {', '.join(unknown)}.",
                "allowed": sorted(CONSTRAINT_KINDS),
            }
        )

    for kind, raw in constraints.items():
        value: Any = raw
        expected = _CONSTRAINT_SHAPES[kind]
        # `bool` is an `int`; `max_hashtags: True` is a mistake, not a number.
        if isinstance(value, bool) is not (expected is bool) or not isinstance(value, expected):
            raise ValidationError({"hard_constraints": f"'{kind}' must be {expected.__name__}."})
        # `isinstance(value, list)` rather than `expected is list`: the same
        # check, and the one a type checker can actually follow.
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            raise ValidationError({"hard_constraints": f"'{kind}' is a list of strings."})

    return constraints


def screen(body: str, constraints: dict[str, Any]) -> ScreenResult:
    """Judge one body against one workspace's policy.

    An empty policy passes everything: a workspace that has not written its
    rules down has not thereby forbidden everything.
    """
    violations: list[str] = []
    for kind, value in constraints.items():
        check = CONSTRAINT_KINDS.get(kind)
        if check is None:  # pragma: no cover — `validate_constraints` refuses these
            continue
        violations.extend(check(body, value))

    return ScreenResult(passed=not violations, violations=violations)
