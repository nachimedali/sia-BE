"""Provider payload → stored columns (C-07, P0-33, P0-38).

One function's worth of judgement, isolated because every downstream number
inherits it: given what a provider sent, what goes in the columns?

**The rule is that absence is null.** Three independent things can make a
metric absent, and all three produce `None`:

* the provider does not cover this platform at all (`registry.resolve` → `None`)
* `MetricCapability` declares the pair unavailable
* the payload simply does not carry the key

None of them produces a zero. A zero is written only when a provider sent the
number `0`, which is a measurement — "nobody clicked" — and is a different
statement from "nobody can tell us how many clicked".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from analytics.models import Availability, MetricCapability, MetricSource
from analytics.providers.base import METRIC_KEYS, MetricsProvider, RawMetricPayload
from analytics.providers.zernio import read_metric
from common.compression import pack
from common.ranking import INTERACTION_WEIGHTS


@dataclass(frozen=True)
class NormalisedMetrics:
    """What a capture writes. Every count is `int | None` on purpose."""

    values: dict[str, int | None]
    engagement_rate: float | None
    reactions: dict[str, int] | None
    availability: str
    provider_key: str
    schema_version: int
    source: str
    raw: dict[str, Any]

    @property
    def is_measured(self) -> bool:
        return self.availability == Availability.MEASURED

    def as_model_fields(self) -> dict[str, Any]:
        return {
            **self.values,
            "engagement_rate": self.engagement_rate,
            "reactions": self.reactions,
            "availability": self.availability,
            "provider_key": self.provider_key,
            "schema_version": self.schema_version,
            "source": self.source,
            "raw": self.raw,
            "raw_payload": pack(self.raw),
        }


def _capability_blocked(provider_key: str, platform: str) -> set[str]:
    """Metrics an admin has declared this provider cannot report here.

    Read as a set of *negatives* rather than a coverage matrix: a pair with no
    row is not thereby declared available, it is simply undeclared, and the
    payload settles it. Only an explicit `available=False` row blocks.
    """
    return set(
        MetricCapability.objects.filter(
            provider_key=provider_key, platform=platform, available=False
        ).values_list("metric_key", flat=True)
    )


def engagement_rate(values: Mapping[str, int | None], *, followers: int | None) -> float | None:
    """Weighted interactions ÷ impressions, or ÷ followers where the platform
    reports no impressions (§8.9).

    `None` — not `0.0` — when neither denominator exists, and `None` when no
    interaction metric was reported at all. The old version returned `0.0` for
    both, which put "we cannot measure this post" into the same bucket as "this
    post earned nothing" in every percentile that ranks them together.
    """
    contributions = [
        weight * value
        for field, weight in INTERACTION_WEIGHTS.items()
        if (value := values.get(field)) is not None
    ]
    if not contributions:
        return None

    denominator = values.get("impressions") or followers
    if not denominator:
        return None
    return sum(contributions) / denominator


def normalise(
    payload: RawMetricPayload,
    *,
    provider: MetricsProvider | None,
    followers: int | None,
    reactions: dict[str, int] | None = None,
) -> NormalisedMetrics:
    """The only place a provider payload becomes columns.

    A `provider` of `None` means nobody covers this platform — A-19's case —
    and yields an `UNAVAILABLE` row carrying no values at all.
    """
    source = MetricSource.FAKE if payload.provider_key == "fake" else MetricSource.PROVIDER
    empty: dict[str, int | None] = dict.fromkeys(METRIC_KEYS)

    if provider is None or not payload.is_measured:
        return NormalisedMetrics(
            values=empty,
            engagement_rate=None,
            reactions=None,
            availability=payload.availability if provider is not None else Availability.UNAVAILABLE,
            provider_key=payload.provider_key,
            schema_version=payload.schema_version,
            source=source,
            raw=payload.body,
        )

    blocked = _capability_blocked(payload.provider_key, payload.platform)
    values: dict[str, int | None] = {
        metric: (
            None
            if metric in blocked or not provider.supports(payload.platform, metric)
            else read_metric(payload.body, metric)
        )
        for metric in METRIC_KEYS
    }

    # A payload that answered nothing is not a measurement of nothing. This is
    # the case a 200 with an empty body produces, and it must not become a row
    # of six zeros.
    availability = (
        Availability.MEASURED
        if any(value is not None for value in values.values())
        else Availability.UNAVAILABLE
    )

    return NormalisedMetrics(
        values=values,
        reactions=reactions,
        engagement_rate=(
            engagement_rate(values, followers=followers)
            if availability == Availability.MEASURED
            else None
        ),
        availability=availability,
        provider_key=payload.provider_key,
        schema_version=payload.schema_version,
        source=source,
        raw=payload.body,
    )
