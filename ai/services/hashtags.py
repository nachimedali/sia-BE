"""Hashtag suggestions, ranked against the category corpus (P1-13).

**Counted, not generated.** The useful answer to "what should I tag this" is
*which tags are actually working in this vertical right now* — a fact, drawn
from the same shared corpus the trend engine already harvests (D11). A model
asked to invent hashtags produces plausible ones, which is strictly worse than
observed ones and costs credits to be wrong.

That makes this a deliberate departure from BUILD-PLAN Phase 1's "all go
through the existing pipeline": there is no provider call, so there is nothing
for the quality gate to judge and nothing to debit. Recorded on the task rather
than made quietly.

Opportunistic and read-only, like every other grounding signal here: a
workspace with no category, or a category with no corpus, gets an empty list
rather than an error.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from common.text import HASHTAG_RE
from trends.models import TrendItem
from trends.services.extraction import WINDOW_DAYS

if TYPE_CHECKING:
    from workspaces.models import Workspace

#: Enough to fill a picker without turning it into a wall. Not a commercial
#: number — nothing is sold by the hashtag — so it lives here rather than on a
#: `Plan` row (I8).
DEFAULT_LIMIT = 25

#: Below this a "trend" is one person's habit. The same reasoning as the
#: cluster-singleton rule in the trend pipeline: one observation is an anecdote.
MIN_OCCURRENCES = 2


@dataclass(frozen=True)
class RankedHashtag:
    tag: str
    #: How many corpus posts in the window used it. **The evidence**, returned
    #: rather than hidden: a ranked list with no counts is an opinion the user
    #: cannot check, and this is a statistic computed in code (Part 7 rule 15).
    count: int


def rank_for_workspace(
    workspace: Workspace, *, limit: int = DEFAULT_LIMIT, now: dt.datetime | None = None
) -> list[RankedHashtag]:
    if workspace.category_id is None:
        return []

    since = (now or timezone.now()) - dt.timedelta(days=WINDOW_DAYS)
    bodies = TrendItem.objects.filter(
        source__category_id=workspace.category_id,
        posted_at__gte=since,
        excluded_reason="",
    ).values_list("body", flat=True)

    # Counted per *item*, not per occurrence: a post that says #ceramics four
    # times is one post using it, and counting mentions would let a single
    # spammy caption dominate the whole vertical's ranking.
    counter: Counter[str] = Counter()
    for body in bodies:
        counter.update({tag.lower() for tag in HASHTAG_RE.findall(body or "")})

    # Sorted explicitly, **including the tie-break**. `Counter.most_common()`
    # leaves equal counts in insertion order, which here is Postgres's row
    # order for an unordered queryset — so two tags used by the same number of
    # posts would swap places between requests, and the 25th place would drop
    # in and out of the list for no reason the user could see. Alphabetical is
    # arbitrary; being stable is not.
    ranked = [
        RankedHashtag(tag=tag, count=count)
        for tag, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if count >= MIN_OCCURRENCES
    ]
    return ranked[:limit]
