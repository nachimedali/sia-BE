"""Trend stage 6 — ranked clusters become candidates (C-10, P5-14).

The trend pipeline ran to stage 5 (Rank) and stopped. Stage 6 is where it
finally touches the product: the clusters a category is talking about, filtered
through **this workspace's** topic posture, become proposals a person can say
yes to.

**Filtered, not merely sorted.** A brand that has said it avoids a topic should
not be shown that topic ranked third — `topic_posture.avoid` is a refusal, and
treating it as a mild preference is how a workspace ends up declining the same
suggestion every week.
"""

from __future__ import annotations

import re
from typing import Any

from taste.models import TasteProfile

#: How many ranked clusters to consider before posture filtering. Bounded
#: because the corpus is category-wide: without a cap, a busy vertical would
#: hand every workspace the same hundred clusters to wade through.
MAX_CONSIDERED = 25


def _mentions(text: str, term: str) -> bool:
    """Word-boundary match, like screening's.

    A brand avoiding "gin" should not have every "beginning" filtered out —
    a posture that silently drops the wrong clusters is worse than no posture,
    because the user cannot see what they are missing.
    """
    return bool(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE))


def eligible_clusters(profile: TasteProfile, clusters: Any) -> list[Any]:
    """The clusters this brand would actually post about, best first.

    Three passes, in this order and for different reasons:

    * **avoid** removes — a stated refusal is a refusal;
    * **favour** promotes — a stated interest is a preference, not a filter, so
      a favoured topic sorts first but an unfavoured one is still offered;
    * the composite rank orders the rest, which is what the trend engine
      already computed and there is no reason to recompute here.
    """
    posture = profile.topic_posture or {}
    avoid = [str(term) for term in posture.get("avoid", [])]
    favour = [str(term) for term in posture.get("favour", [])]

    kept = [
        cluster
        for cluster in list(clusters)[:MAX_CONSIDERED]
        if not any(_mentions(cluster.label, term) for term in avoid)
    ]

    def sort_key(cluster: Any) -> tuple[int, float]:
        favoured = any(_mentions(cluster.label, term) for term in favour)
        # Negated so a higher composite sorts earlier — the trend engine's own
        # ranking, preserved rather than re-derived.
        return (0 if favoured else 1, -cluster.composite_score)

    return sorted(kept, key=sort_key)


def candidates_from_trends(
    *, workspace: Any, profile: TasteProfile, clusters: Any, limit: int = 3, **kwargs: Any
) -> list[Any]:
    """Stage 6 proper: ranked clusters → candidates in the review queue.

    Each one still goes through `propose`, so hard-constraint screening applies
    exactly as it does to anything else (P5-G1). A trend is a reason to write
    something, never a reason to skip the brand's own policy.
    """
    from taste.services.candidates import propose

    return [
        propose(
            workspace=workspace,
            profile=profile,
            payload={
                "master_body": cluster.label,
                # Kept so a reviewer can see *why* this was suggested. A
                # proposal that cannot explain itself gets rejected on
                # suspicion, which teaches the taste model nothing useful.
                "trend": {
                    "cluster": cluster.pk,
                    "label": cluster.label,
                    "platform": cluster.platform,
                    "composite_score": cluster.composite_score,
                },
            },
            **kwargs,
        )
        for cluster in eligible_clusters(profile, clusters)[:limit]
    ]
