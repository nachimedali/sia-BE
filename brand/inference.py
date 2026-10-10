"""What a website implies — audience, voice, category, competitors (S1-03).

A port with two adapters: the configured LLM (`ai.providers.llm_text`) and a
deterministic fake. **Both answers are checked before anything uses them**:

* the voice preset must be one of `BrandVoice`, the category one of the
  categories offered, the business type one of `BusinessType`;
* the quote must appear **verbatim** in the text that was read — a model asked
  for a line "from the site" will otherwise write a plausible one, and a brand
  kit quoting a sentence the brand never wrote is worse than no quote;
* competitors are always `relationship: "inferred"` — the import never claims
  to have verified who a brand competes with.

Anything that fails a check is dropped, never repaired into something else.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Protocol

from django.conf import settings

from workspaces.models import BrandVoice, BusinessType

_SYSTEM = (
    "You read a company's website text and describe the brand for a marketing tool. "
    "Answer with JSON only, no prose, in this exact shape: "
    '{"audience": {"summary": str, "segments": [str]}, '
    '"voice": {"preset": "WARM"|"SHARP"|"PLAYFUL"|"EDITORIAL", '
    '"descriptors": [str, str, str], "quote": str}, '
    '"category": str|null, "business_type": "D2C"|"SERVICE"|"CREATOR"|null, '
    '"competitors": [{"name": str, "domain": str, "why": str}]}. '
    "Rules: the audience summary is one sentence about who buys, from what the text says. "
    "Segments are 2-5 short labels. Descriptors are three single adjectives for the tone. "
    "The quote MUST be one sentence copied exactly from the text. "
    "The category MUST be copied exactly from the list given, or null. "
    "Competitors: at most 4 real brands you are confident exist in the same market; [] if unsure. "
    "Never invent facts, prices, numbers or customers."
)


class BrandInference(Protocol):
    def infer(
        self, *, name: str, text: str, categories: list[str], products: list[str]
    ) -> dict[str, Any]: ...


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def validate(raw: Any, *, text: str, categories: list[str]) -> dict[str, Any]:
    """Keep what passes, drop what does not. Returns only checked fields."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    haystack = _norm(text)

    audience = raw.get("audience")
    if isinstance(audience, dict):
        summary = audience.get("summary")
        segments = [
            s.strip()[:40]
            for s in audience.get("segments") or []
            if isinstance(s, str) and s.strip()
        ]
        if isinstance(summary, str) and summary.strip():
            out["audience"] = {"summary": summary.strip()[:400], "segments": segments[:5]}

    voice = raw.get("voice")
    if isinstance(voice, dict):
        checked: dict[str, Any] = {}
        if voice.get("preset") in BrandVoice.values:
            checked["preset"] = voice["preset"]
        descriptors = [
            d.strip().lower()[:24]
            for d in voice.get("descriptors") or []
            if isinstance(d, str) and d.strip()
        ]
        if descriptors:
            checked["descriptors"] = descriptors[:3]
        quote = voice.get("quote")
        if isinstance(quote, str) and quote.strip() and _norm(quote) in haystack:
            checked["quote"] = quote.strip()[:240]
        if checked:
            out["voice"] = checked

    category = raw.get("category")
    if isinstance(category, str):
        match = next((c for c in categories if c.casefold() == category.strip().casefold()), None)
        if match:
            out["category"] = match

    if raw.get("business_type") in BusinessType.values:
        out["business_type"] = raw["business_type"]

    competitors: list[dict[str, str]] = []
    for row in raw.get("competitors") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        domain = (
            str(row.get("domain") or "")
            .strip()
            .lower()
            .removeprefix("https://")
            .removeprefix("http://")
            .strip("/")
        )
        if not isinstance(name, str) or not name.strip():
            continue
        if domain and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
            domain = ""
        competitors.append(
            {
                "name": name.strip()[:80],
                "domain": domain,
                "why": str(row.get("why") or "").strip()[:200],
                "relationship": "inferred",
            }
        )
    if competitors:
        out["competitors"] = competitors[:4]
    return out


def _json_in(body: str) -> Any:
    body = body.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start : end + 1])
    except ValueError:
        return None


class LLMBrandInference:
    def infer(
        self, *, name: str, text: str, categories: list[str], products: list[str]
    ) -> dict[str, Any]:
        from ai.providers.llm_text import get_text_provider

        prompt = json.dumps(
            {
                "brand": name,
                "categories": categories,
                "products": products[:12],
                "website_text": text,
            },
            ensure_ascii=False,
        )
        try:
            result = get_text_provider().generate(system=_SYSTEM, prompt=prompt, n=1)
        except Exception:  # a provider failure leaves the inferred sections unknown
            return {}
        body = result.variants[0].body if result.variants else ""
        return validate(_json_in(body), text=text, categories=categories)


_ADJECTIVES = (
    "warm",
    "honest",
    "simple",
    "bold",
    "playful",
    "fresh",
    "crafted",
    "local",
    "premium",
    "calm",
    "generous",
    "rooted",
    "precise",
    "curious",
    "friendly",
    "sunny",
    "colourful",
    "patient",
    "clean",
    "modern",
)


class FakeBrandInference:
    """Deterministic, and derived only from the text it is given."""

    def infer(
        self, *, name: str, text: str, categories: list[str], products: list[str]
    ) -> dict[str, Any]:
        lowered = text.casefold()
        words = Counter(re.findall(r"[a-zà-ÿ]+", lowered))
        raw: dict[str, Any] = {}

        sentences = [
            s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 6 <= len(s.split()) <= 28
        ]
        audience = next((s for s in sentences if " for " in f" {s.casefold()} "), None)
        if audience:
            raw["audience"] = {"summary": audience, "segments": []}

        descriptors = [a for a in _ADJECTIVES if words[a]][:3]
        exclaim = text.count("!")
        long_sentences = sum(1 for s in sentences if len(s.split()) > 18)
        preset = "PLAYFUL" if exclaim >= 3 else "EDITORIAL" if long_sentences >= 3 else "WARM"
        raw["voice"] = {
            "preset": preset,
            "descriptors": descriptors,
            "quote": sentences[0] if sentences else "",
        }

        best, score = None, 0
        for category in categories:
            terms = [t for t in re.findall(r"[a-z]+", category.casefold()) if len(t) > 3]
            hits = sum(words[t] + words[t.rstrip("s")] for t in terms)
            if hits > score:
                best, score = category, hits
        if best:
            raw["category"] = best

        if products:
            raw["business_type"] = "D2C"
        elif re.search(r"\b(agency|clients|services)\b", lowered):
            raw["business_type"] = "SERVICE"
        return validate(raw, text=text, categories=categories)


def get_brand_inference() -> BrandInference:
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        return FakeBrandInference()
    return LLMBrandInference()
