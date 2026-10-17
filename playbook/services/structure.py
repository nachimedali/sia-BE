"""The "what makes it work" facts about a post, computed in code (rule 5).

The template's cards say things like "a cut every 1.2 s" and "no face in the
first shot". Nothing either provider publishes supports a claim like that, and
a model asked to guess would invent it. So the card says only what can be
measured: the format and length, how the title opens, and how fast the post is
gaining views and reactions **compared with the median post on the same wall**
— ratios over numbers the provider reported, never a prediction.

A missing number produces no sentence rather than a guessed one.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING

from playbook.models import PostFormat

if TYPE_CHECKING:
    from playbook.models import PlaybookPost

MAX_FACTS = 5
#: A ratio this close to 1 is "about the median" — saying "1.1x faster" about
#: a difference that small would claim a precision the data does not carry.
NEUTRAL_BAND = 0.2


def clock(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes}:{rest:02d}"


def _ratio(value: float | None, median: float | None, *, what: str, faster: str) -> str | None:
    if value is None or not median:
        return None
    ratio = value / median
    if ratio >= 1 + NEUTRAL_BAND:
        return f"{what} {ratio:.1f}× {faster} the median post on this wall"  # noqa: RUF001 — shown to people
    if ratio <= 1 - NEUTRAL_BAND:
        return f"{what} below the median post on this wall ({ratio:.1f}×)"  # noqa: RUF001 — shown to people
    return f"{what} at about the median pace of this wall"


def describe(
    post: PlaybookPost,
    *,
    median_velocity: float | None,
    median_engagement: float | None,
    now: dt.datetime,
) -> list[str]:
    facts: list[str] = []
    if post.format == PostFormat.SEARCH:
        if post.traffic_label:
            facts.append(f"About {post.traffic_label} searches so far (Google's own range)")
        hours = max(1, int((now - post.posted_at).total_seconds() // 3600))
        facts.append(f"Rising for {hours} h")
        if post.news:
            count = len(post.news)
            facts.append(f"{count} news {'story' if count == 1 else 'stories'} attached")
        return facts[:MAX_FACTS]

    if post.duration_seconds:
        kind = "Short video" if post.format == PostFormat.SHORT else "Video"
        facts.append(f"{kind}, {clock(post.duration_seconds)}")
    title = post.title.strip()
    if "?" in title:
        facts.append("The title asks a question")
    elif re.match(r"^\d", title):
        facts.append("The title leads with a number")
    if title:
        words = len(title.split())
        facts.append(f"A {words}-word title")
    pace = _ratio(post.velocity, median_velocity, what="Gaining views", faster="faster than")
    if pace:
        facts.append(pace)
    reactions = _ratio(
        post.engagement, median_engagement, what="Likes and comments per view", faster="above"
    )
    if reactions:
        facts.append(reactions)
    elif post.likes is None and post.views is not None:
        facts.append("The like count is hidden by the creator")
    return facts[:MAX_FACTS]
