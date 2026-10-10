"""Deterministic stand-ins for the S4 ports.

**The fake policy reader is a small rule table, not a model.** It knows a
handful of phrasings that the major platforms' ad rules refuse or restrict —
cure claims, guaranteed money, "before and after", calling out a reader's
personal attributes, unverifiable superlatives — so the checks, the gate and
the screens can be exercised end to end. Its clauses are this product's own
summaries of those rules, attributed to the page they summarise; they are not
quotations, and every result it returns carries `provider="fake"`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from checks.ports import OcrResult, PolicyFinding, PolicyResult, TextBox

META_HEALTH = "https://transparency.meta.com/policies/ad-standards/"


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    status: str
    title: str
    clause: str
    source: str
    replacement: str


_RULES: tuple[_Rule, ...] = (
    _Rule(
        re.compile(
            r"\b(cures?|heals?|treats?)\s+(cancer|diabetes|disease|covid|arthritis)\b", re.I
        ),
        "BLOCK",
        "Medical claim",
        "Ads may not claim that a product prevents, treats or cures a disease.",
        META_HEALTH,
        "supports your everyday wellbeing",
    ),
    _Rule(
        re.compile(r"\b(guaranteed|risk[- ]free)\s+(returns?|profits?|income|weight loss)\b", re.I),
        "BLOCK",
        "Guaranteed outcome",
        "Ads may not promise guaranteed financial or health results.",
        META_HEALTH,
        "results that vary from person to person",
    ),
    _Rule(
        re.compile(r"\bbefore\s+(and|&)\s+after\b", re.I),
        "FIX",
        "Before-and-after framing",
        "Ads should not use before-and-after comparisons to imply an unrealistic result.",
        META_HEALTH,
        "our customers' experience",
    ),
    _Rule(
        re.compile(r"\bare you (overweight|in debt|depressed|single|struggling)\b", re.I),
        "FIX",
        "Personal attributes",
        "Ads may not assert or imply personal attributes of the person reading them.",
        META_HEALTH,
        "for anyone who wants a change",
    ),
    _Rule(
        re.compile(r"(#1|number one|best in the world|world'?s best)", re.I),
        "FIX",
        "Unverifiable superlative",
        "Claims of being the best or first need evidence the reader can check.",
        META_HEALTH,
        "a favourite with our customers",
    ),
)


class FakePolicy:
    def review(self, *, text: str, platform: str, category: str) -> PolicyResult:
        findings: list[PolicyFinding] = []
        for rule in _RULES:
            match = rule.pattern.search(text)
            if match is None:
                continue
            findings.append(
                PolicyFinding(
                    status=rule.status,
                    title=rule.title,
                    clause=rule.clause,
                    source=rule.source,
                    excerpt=match.group(0),
                    rewrite=rule.pattern.sub(rule.replacement, text),
                )
            )
        return PolicyResult(findings=findings, provider="fake")


class FakeOcr:
    """Reads nothing: the fake image provider draws no words. A test that
    needs text in a picture installs its own reader with `set_ocr_override`."""

    def read(self, image: bytes) -> OcrResult:
        return OcrResult(boxes=[], provider="fake")


class ScriptedOcr:
    """Tests: always reads the boxes it was given."""

    def __init__(self, boxes: list[TextBox]) -> None:
        self.boxes = boxes
        self.calls = 0

    def read(self, image: bytes) -> OcrResult:
        self.calls += 1
        return OcrResult(boxes=self.boxes, provider="scripted")
