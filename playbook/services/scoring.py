"""The trend score: how fast a post is gaining attention, ranked on its wall.

* **Velocity** — views per hour since posting for a video; for a search, the
  floor of Google's traffic bucket per hour since it started rising. Hours are
  floored at `MIN_HOURS`, so a post two minutes old with forty views does not
  out-trend everything published yesterday.
* **Engagement** — likes plus comments per hundred views. Null unless the
  provider reported all three: a hidden like count is not a zero (rule 4).
* **Trend score** — the velocity's percentile, 0 to 100, **within its own
  provider and market**. Views per hour and searches per hour are different
  units, and ranking them in one pool would let whichever is larger win every
  slot — the same reason `trends/` partitions its percentiles by source kind.
  Fixture rows are ranked apart from real ones, so a sample can never move a
  real post's score.

Computed in code from stored columns (rule 5), and recomputable at any time.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from django.utils import timezone

from playbook.models import PlaybookPost, PostFormat
from playbook.services import structure

WINDOW_DAYS = 30
MIN_HOURS = 6.0


def _velocity(post: PlaybookPost, now: dt.datetime) -> float | None:
    hours = max(MIN_HOURS, (now - post.posted_at).total_seconds() / 3600)
    reach = post.traffic_floor if post.format == PostFormat.SEARCH else post.views
    return None if reach is None else reach / hours


def _engagement(post: PlaybookPost) -> float | None:
    if not post.views or post.likes is None or post.comments is None:
        return None
    return round((post.likes + post.comments) / post.views * 100, 2)


def _percentiles(values: list[float]) -> list[int | None]:
    """Mid-rank percentile of each value among `values`. A wall of one has no
    rank to give, so it gets none rather than a flattering 100."""
    if len(values) < 2:
        return [None] * len(values)
    ordered = sorted(values)
    out: list[int | None] = []
    for value in values:
        below = sum(1 for other in ordered if other < value)
        equal = sum(1 for other in ordered if other == value)
        out.append(round(100 * (below + (equal - 1) / 2) / (len(values) - 1)))
    return out


def rescore(region: str, *, now: dt.datetime | None = None) -> int:
    now = now or timezone.now()
    posts = list(
        PlaybookPost.objects.filter(
            region=region, posted_at__gte=now - dt.timedelta(days=WINDOW_DAYS)
        )
    )
    partitions: dict[tuple[str, bool], list[PlaybookPost]] = defaultdict(list)
    for post in posts:
        post.velocity = _velocity(post, now)
        post.engagement = _engagement(post)
        partitions[(post.provider, post.is_fake)].append(post)

    for members in partitions.values():
        ranked = [post for post in members if post.velocity is not None]
        for post, score in zip(
            ranked, _percentiles([post.velocity or 0.0 for post in ranked]), strict=True
        ):
            post.trend_score = score
        for post in members:
            if post.velocity is None:
                post.trend_score = None
        velocities = [post.velocity for post in members if post.velocity is not None]
        engagements = [post.engagement for post in members if post.engagement is not None]
        median_velocity = statistics.median(velocities) if velocities else None
        median_engagement = statistics.median(engagements) if engagements else None
        for post in members:
            post.structure = structure.describe(
                post,
                median_velocity=median_velocity,
                median_engagement=median_engagement,
                now=now,
            )

    PlaybookPost.objects.bulk_update(
        posts, ["velocity", "engagement", "trend_score", "structure"], batch_size=500
    )
    return len(posts)
