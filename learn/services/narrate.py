"""Narration (P7-08 … P7-10) — prose rendered from numbers, and checked.

**The statistics are computed in `statistics.py`; this module only renders
them.** Part 7 rule 15. The narrator gets a structured payload and a rendering
instruction — no tool access, no retrieval, nothing to look anything up with.

Why the output is validated rather than merely instructed:

*The instruction almost always works, and the digest is read as authoritative
exactly on the occasions it does not.* A prompt asking a model not to invent
figures is a strong prior, not a guarantee, and "strong prior" is the wrong
foundation for a document a customer forwards to a client. So compliance is
checked mechanically afterwards — a numeral in the prose that is not in the
payload cannot have come from the data — and a failing render is **replaced**,
never shown with a caveat.

*Predictions are refused even when their arithmetic is sound.* P7-10. `+20%` may
be a correct multiplication of two real figures and is still forbidden, because
upstream metric quality is uneven and unverifiable per platform. "Carousels led
in 14 of 20 posts" degrades gracefully under bad data; "carousels will lift
reach 20%" does not — it is simply wrong, and it was the product that said it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Protocol

from learn.models import Confidence, NarrationSource
from learn.services.statistics import SegmentStat

#: Every numeral shape prose can carry: integers, decimals, and thousands
#: separators. Deliberately greedy — a token this misses is a token that skips
#: the check, so over-matching is the safe direction.
_NUMERAL = re.compile(r"\d[\d,.]*")

#: P7-10's forbidden shapes. Two families: an explicit signed delta, and the
#: vocabulary of forecast. Both are matched case-insensitively on word
#: boundaries so "unexpected" does not trip "expect".
_PREDICTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile("[+\u2212-]\\s?\\d+(?:[.,]\\d+)?\\s?%"),
    re.compile(r"\bwill\s+(?:increase|rise|improve|grow|climb|lift|drop|fall|reach)\b", re.I),
    re.compile(r"\b(?:expect|expects|expected|expecting)\b", re.I),
    re.compile(r"\b(?:project|projects|projected|projection)\b", re.I),
    re.compile(r"\b(?:forecast|forecasts|forecasted)\b", re.I),
    re.compile(r"\b(?:predict|predicts|predicted|prediction)\b", re.I),
    re.compile(r"\b(?:should|would|could)\s+(?:increase|rise|improve|grow|climb|lift)\b", re.I),
    re.compile(r"\bestimated\s+(?:lift|gain|increase|uplift)\b", re.I),
)

#: How a dimension is said in a sentence. A lookup rather than a prettifier,
#: because "posting_hour" → "posting hour" is right and "media_kind" → "media
#: kind" is not what anybody calls it.
_DIMENSION_NOUNS: dict[str, str] = {
    "format": "format",
    "platform": "platform",
    "posting_hour": "time of day",
    "media_kind": "media type",
    "length_band": "caption length",
    "topic": "topic",
    "tone": "tone",
}

SYSTEM_PROMPT = (
    "You write a short read-out of a social media account's recent results. "
    "You are given a JSON payload of already-computed statistics. "
    "Use ONLY the numbers in that payload; never compute, estimate, round or "
    "invent a figure, and never state what will happen next. Describe what was "
    "observed, comparatively, and name the confidence and sample size. "
    "Three to six sentences, plain prose, no headings, no bullet points."
)

PROMPT_TEMPLATE_VERSION = "learn-digest-v1"


class _Narrator(Protocol):
    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any: ...


def build_payload(
    stats: list[SegmentStat],
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    campaign_name: str = "",
) -> dict[str, Any]:
    """The narrator's entire world: numbers, grades and segment labels.

    Ordered strongest-first so a truncated render still leads with the finding
    that carries the most evidence, and so the template below can rely on the
    ordering rather than re-deriving it.
    """
    order = {Confidence.STRONG: 0, Confidence.EMERGING: 1, Confidence.INSUFFICIENT: 2}
    ranked = sorted(stats, key=lambda row: (order[Confidence(row.confidence)], -row.sample_size))

    return {
        "campaign": campaign_name,
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "findings": [
            {
                "segment": row.value,
                "dimension": row.dimension,
                "dimension_noun": _DIMENSION_NOUNS.get(row.dimension, row.dimension),
                "confidence": str(row.confidence),
                "sample_size": row.sample_size,
                "baseline_size": row.baseline_size,
                "led_in": row.led_in,
                "campaigns_observed": row.campaigns_observed,
                "excluded_reason": row.excluded_reason,
            }
            for row in ranked
        ],
    }


def numerals_are_grounded(text: str, payload: dict[str, Any]) -> bool:
    """P7-09. Every numeral in `text` must appear in `payload`.

    Compared as digit strings with separators stripped, so "1,200" in prose
    matches 1200 in the payload. Dates in the payload make their own components
    available, which is intended — a sentence naming the window's month is
    rendering the payload, not adding to it.
    """
    allowed = {_canonical(token) for token in _NUMERAL.findall(json.dumps(payload))}
    # The window dates arrive as `2026-01-01`; a reader writing "1 January"
    # needs the parts, not only the whole.
    for key in ("window_start", "window_end"):
        for part in str(payload.get(key, "")).split("-"):
            if part:
                allowed.add(_canonical(part))

    return all(_canonical(token) in allowed for token in _NUMERAL.findall(text))


def _canonical(token: str) -> str:
    """`1,200.0` and `01200` both reduce to the same comparable form."""
    cleaned = token.replace(",", "").rstrip(".")
    if "." in cleaned:
        whole, _, fraction = cleaned.partition(".")
        fraction = fraction.rstrip("0")
        cleaned = f"{whole}.{fraction}" if fraction else whole
    return cleaned.lstrip("0") or "0"


def reads_as_a_prediction(text: str) -> bool:
    """P7-10. True for anything that states a future outcome."""
    return any(pattern.search(text) for pattern in _PREDICTION_PATTERNS)


def template_narration(payload: dict[str, Any]) -> str:
    """Deterministic prose, built only from the payload.

    This is what a customer reads whenever the provider is down or wrong, so it
    is written to be publishable rather than to be a placeholder. It is also
    held to the same rules it enforces — `test_every_numeral_it_prints_is_in_
    the_payload` — because a fallback that could invent a figure would be
    trusted precisely for being the thing invalid renders are replaced with.
    """
    findings = [row for row in payload.get("findings", []) if not row.get("excluded_reason")]
    strong = [row for row in findings if row["confidence"] == Confidence.STRONG]
    emerging = [row for row in findings if row["confidence"] == Confidence.EMERGING]

    sentences: list[str] = []
    for row in strong[:3]:
        sentences.append(
            f"Your {row['segment']} {row['dimension_noun']} led the rest of this window "
            f"in {row['led_in']} of {row['sample_size']} posts, and held across "
            f"{row['campaigns_observed']} campaigns."
        )
    for row in emerging[:2]:
        sentences.append(
            f"{row['segment'].capitalize()} looks like it is doing better, on "
            f"{row['sample_size']} posts — enough to be worth testing, not yet enough to "
            f"act on."
        )

    if not sentences:
        excluded = [row for row in payload.get("findings", []) if row.get("excluded_reason")]
        counted = sum(int(row["sample_size"]) for row in payload.get("findings", []))
        sentences.append(
            "There is not enough data yet to say what is working. "
            f"This window covers {counted} measured posts across all segments; "
            "findings appear once a segment reaches eight."
        )
        if excluded:
            sentences.append(
                f"{len(excluded)} segments were left out because {excluded[0]['excluded_reason']}."
            )

    return " ".join(sentences)


def narrate(
    payload: dict[str, Any],
    *,
    provider: _Narrator,
    model: str | None = None,
) -> tuple[str, str]:
    """Render the payload, validate the result, and fall back if it fails.

    Returns the prose and which half produced it. The caller records the source
    on the digest: a template narration is a *correct* digest whose provider
    declined or overreached, and a rising fallback rate is something to notice
    rather than an error to swallow.
    """
    fallback = template_narration(payload)

    try:
        result = provider.generate(
            system=SYSTEM_PROMPT,
            prompt=json.dumps(payload, sort_keys=True),
            n=1,
            model=model,
        )
        candidate = (result.variants[0].body or "").strip() if result.variants else ""
    except Exception:
        # Any provider failure is a fallback, never a lost digest.
        return fallback, NarrationSource.TEMPLATE

    if not candidate:
        return fallback, NarrationSource.TEMPLATE
    if reads_as_a_prediction(candidate):
        return fallback, NarrationSource.TEMPLATE
    if not numerals_are_grounded(candidate, payload):
        return fallback, NarrationSource.TEMPLATE

    return candidate, NarrationSource.MODEL
