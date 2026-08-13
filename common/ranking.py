"""Percentile ranking, shared by everything that scores a population.

Two callers with the same question: `trends.services.scoring` ranks the corpus a
post is generated *from*, `analytics.services.signals` ranks the post that
results. They must agree — the two feed the same prompt (design.md §8.3), so a
tie broken one way there and another way here would mean the generator was
grounded in one opinion and measured by another.

Tie handling is the subtle part and the reason this is shared rather than
written twice: a window where nine items report no metrics at all must leave
those nine *tied* at the bottom, not ordered by whichever happened to arrive
first.
"""

from __future__ import annotations


def percentiles(values: list[float]) -> list[float]:
    """Rank each value in [0, 1], ties sharing the mean of the ranks they span.

    A single value ranks 1.0: it is, trivially, the best of what there is.
    Callers that consider one observation insufficient evidence enforce their
    own floor — `analytics` needs five posts before it will show a percentile
    at all — rather than this returning something evasive.
    """
    if len(values) <= 1:
        return [1.0] * len(values)

    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        shared = (position + end) / 2 / (len(values) - 1)
        for index in order[position : end + 1]:
            ranks[index] = shared
        position = end + 1
    return ranks


#: Weighted interactions, for every engagement figure in the product.
#:
#: A save is worth more than a like because it is the strongest signal a viewer
#: intends to come back; a share is next, because it costs the sharer
#: reputation. Shared between the trend scorer and the analytics capture for the
#: same reason `percentiles` is: retuning this in one place and not the other
#: would score a workspace's own posts on a different scale from the corpus they
#: are compared against.
#:
#: Not an I8 quota — no plan may change it. It is a model parameter, tuned by
#: whoever tunes the ranking.
INTERACTION_WEIGHTS = {"likes": 1.0, "comments": 3.0, "shares": 5.0, "saves": 6.0}
