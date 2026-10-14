"""Telling a YouTube ad from the rest of YouTube.

YouTube has no "ads only" filter, so ads are recognised two ways: the search is
steered toward them (`search_query`), and a result is kept only if its title or
description carries an ad signal and its title is not a non-ad format — trailer,
review, explainer (`is_ad`). Matching is on whole words, so "Road" is not "ad".

One definition, used by both the trend vendor and the Playbook collector, so the
two never disagree about what an ad is. Both lists can be overridden per source
row (`ad_terms`, `exclude_terms`), so retuning is an admin edit, not a deploy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

AD_TERMS = (
    "commercial",
    "advert",
    "advertisement",
    "tv spot",
    "tv ad",
    "ad campaign",
    "#ad",
    "sponsored",
)
EXCLUDE_TERMS = (
    "trailer",
    "teaser",
    "review",
    "reaction",
    "explained",
    "tutorial",
    "how to",
    "full movie",
    "podcast",
    "interview",
    "gameplay",
    "unboxing",
)


def _pattern(terms: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        r"(?<![\w#])(?:" + "|".join(re.escape(t.lower()) for t in terms) + r")(?!\w)",
        re.IGNORECASE,
    )


def terms_from(config: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """`(ad_terms, exclude_terms)` for a source row, defaults where unset."""
    return (
        list(config.get("ad_terms", AD_TERMS)),
        list(config.get("exclude_terms", EXCLUDE_TERMS)),
    )


def search_query(topic: str, config: Mapping[str, Any]) -> str:
    """The `q` for `search.list`: the topic, any of the ad terms, none of the
    excluded formats. A steer, not a guarantee — `is_ad` is the guarantee."""
    ad_terms, exclude = terms_from(config)
    clause = "(" + "|".join(f'"{t}"' if " " in t else t for t in ad_terms) + ")"
    return " ".join([topic, clause, *(f"-{t.replace(' ', '')}" for t in exclude)]).strip()


def ad_filter(config: Mapping[str, Any]) -> Any:
    """A predicate `(title, description) -> bool` built once per fetch."""
    ad_terms, exclude = terms_from(config)
    is_ad, is_other = _pattern(ad_terms), _pattern(exclude)

    def keep(title: str, description: str = "") -> bool:
        return bool(is_ad.search(f"{title} {description}") and not is_other.search(title))

    return keep
