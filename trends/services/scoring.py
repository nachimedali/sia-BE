"""Stage 3 — score (design.md §8.4).

```
engagement = weighted(likes, shares, comments, saves) / followers
velocity   = engagement / hours_since_posted
recency    = exp(-hours / half_life)
authority  = log(followers), capped
composite  = w1·velocity + w2·recency + w3·authority + w4·engagement
```

**Percentile-normalised within `(category, platform, window)`, and additionally
within source kind.** design.md names the first partition; the second is forced
by what a `(category, platform)` actually contains. A Reddit thread reports no
follower count and a YouTube channel reports a million, so raw engagement is not
comparable across kinds — without the second partition the kind with the most
generous denominator wins every cluster, and "top decile by composite score"
(stage 3's own consumer) stops meaning "the best items" and starts meaning "the
items from one source". Ranking inside each kind and combining the ranks keeps
every source able to contribute its own best.

Weights are module constants rather than `Plan` rows: I8 governs quotas, prices
and credit costs — things a customer's money buys. These are model parameters,
tuned by whoever is tuning the ranking, and a plan cannot change them.
"""

from __future__ import annotations

import math
from collections import defaultdict

from django.utils import timezone

from common.ranking import INTERACTION_WEIGHTS, percentiles
from trends.models import TrendItem

#: `composite = w1·velocity + w2·recency + w3·authority + w4·engagement`.
#: Velocity leads: a post pulling hard *now* is the thing a calendar can still
#: act on, which is what this corpus is for.
COMPOSITE_WEIGHTS = {"velocity": 0.40, "recency": 0.25, "authority": 0.10, "engagement": 0.25}

#: Recency half-life. Three days: shorter and a Monday extraction cannot see the
#: previous week at all, longer and a fortnight-old post outranks yesterday's.
RECENCY_HALF_LIFE_HOURS = 72.0

#: `log(followers), capped` — the cap is the point. Authority should separate a
#: hobbyist from an established account, not hand the ranking to whichever brand
#: is biggest.
AUTHORITY_CAP = math.log(1_000_000)

#: Floor on both the engagement denominator and the velocity one, so a
#: follower-less source or a post from ten minutes ago cannot divide its way to
#: an unbounded score.
MIN_AUDIENCE = 1_000.0
MIN_AGE_HOURS = 1.0


def _interactions(item: TrendItem) -> float:
    return sum(
        weight * float(item.raw_metrics.get(key, 0) or 0)
        for key, weight in INTERACTION_WEIGHTS.items()
    )


def score(items: list[TrendItem]) -> list[TrendItem]:
    """Scores every item in place and persists the five columns.

    Returns the same list, ordered best first, so stage 3's consumer (the top
    decile) and stage 4's (clustering) can both take it as given.
    """
    if not items:
        return []

    now = timezone.now()
    raw: dict[int, dict[str, float]] = {}
    for item in items:
        hours = max((now - item.posted_at).total_seconds() / 3600, MIN_AGE_HOURS)
        engagement = _interactions(item) / max(float(item.author_followers), MIN_AUDIENCE)
        raw[item.pk] = {
            "engagement": engagement,
            "velocity": engagement / hours,
            "recency": math.exp(-hours / RECENCY_HALF_LIFE_HOURS),
            "authority": min(math.log(max(item.author_followers, 1)), AUTHORITY_CAP),
        }

    # Percentile per component, per source kind (see the module docstring).
    normalised: dict[int, dict[str, float]] = defaultdict(dict)
    by_kind: dict[str, list[TrendItem]] = defaultdict(list)
    for item in items:
        by_kind[item.source.kind].append(item)

    for kind_items in by_kind.values():
        for component in COMPOSITE_WEIGHTS:
            component_ranks = percentiles([raw[item.pk][component] for item in kind_items])
            for item, rank in zip(kind_items, component_ranks, strict=True):
                normalised[item.pk][component] = rank

    for item in items:
        ranks = normalised[item.pk]
        item.engagement_score = ranks["engagement"]
        item.velocity_score = ranks["velocity"]
        item.recency_score = ranks["recency"]
        item.authority_score = ranks["authority"]
        item.composite_score = sum(
            weight * ranks[name] for name, weight in COMPOSITE_WEIGHTS.items()
        )

    TrendItem.objects.bulk_update(
        items,
        [
            "engagement_score",
            "velocity_score",
            "recency_score",
            "authority_score",
            "composite_score",
        ],
    )
    return sorted(items, key=lambda item: item.composite_score, reverse=True)
