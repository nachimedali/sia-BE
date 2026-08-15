"""Derived signals (design.md §8.9).

Four questions the raw captures cannot answer on their own:

* **How did this post do — for us?** Percentile against *the same account's*
  trailing 90 days. Cross-account comparison at different follower scales is
  meaningless, which is why the denominator is never a global average.
* **When should we post?** Engagement bucketed by `(weekday, hour)`.
* **What should we make?** Engagement grouped by media type, and by `source=AI`
  vs `MANUAL` — the honest self-audit, which tells the user whether the AI is
  actually helping rather than assuming it is.
* **Is it still earning?** The decay slope between the T+72h capture and the
  last one, which is what separates an evergreen post from a spike.

Everything here reads the *latest* capture per target. A post's numbers only go
up, so the last row is its current standing, and comparing latest-to-latest is
the only comparison that treats a week-old post and a month-old post fairly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from django.db.models import Avg, Count, Q
from django.utils import timezone

from analytics.models import Comment, PostMetric, Sentiment
from common.ranking import percentiles
from content.models import Post, PostSource, PostTarget, PostTargetState

#: design.md §8.9: "percentile rank against the same account's trailing 90 days".
TRAILING_WINDOW_DAYS = 90

#: A percentile needs something to rank against. Below this the answer is "not
#: enough history yet" rather than a number that reads as authoritative because
#: it is one of three.
MIN_HISTORY_FOR_PERCENTILE = 5


@dataclass(frozen=True)
class TargetPerformance:
    target_id: int
    post_id: int
    platform: str
    published_at: dt.datetime
    engagement_rate: float
    impressions: int
    likes: int
    comments: int
    #: `None` until the account has `MIN_HISTORY_FOR_PERCENTILE` posts to rank
    #: this one against.
    percentile: float | None = None
    #: Ratio of the final capture's engagement to the T+72h one. Above 1 means
    #: the post kept earning after the spike; at or below 1 means it did not.
    decay_ratio: float = 1.0


@dataclass
class Overview:
    posts: int = 0
    impressions: int = 0
    engagement_rate: float = 0.0
    top: list[TargetPerformance] = field(default_factory=list)
    by_source: dict[str, dict[str, float]] = field(default_factory=dict)
    by_media: dict[str, dict[str, float]] = field(default_factory=dict)
    best_times: list[dict[str, Any]] = field(default_factory=list)


#: One capture, as the six scalars this module actually reads. Tuples rather
#: than model instances, and named columns rather than `SELECT *`, because
#: `PostMetric.raw` holds the whole provider payload: a two-year horizon is
#: ~107 captures per target, so hydrating them in full would drag megabytes of
#: JSON through the ORM to compute one float per target.
_CAPTURE_COLUMNS = (
    "post_target_id",
    "captured_at",
    "engagement_rate",
    "impressions",
    "likes",
    "comments",
)


@dataclass(frozen=True)
class _Capture:
    captured_at: dt.datetime
    engagement_rate: float
    impressions: int
    likes: int
    comments: int


def _captures_by_target(targets: list[PostTarget]) -> dict[int, list[_Capture]]:
    """Every capture for these targets, oldest first, in one query.

    One query rather than two: the newest capture per target — which is a
    target's current standing — is the last element of its own list, so asking
    the database for it separately would be paying twice for data already in
    hand.
    """
    grouped: dict[int, list[_Capture]] = defaultdict(list)
    rows = (
        PostMetric.objects.filter(post_target__in=targets)
        .order_by("post_target_id", "captured_at")
        .values_list(*_CAPTURE_COLUMNS)
    )
    for target_id, captured_at, rate, impressions, likes, comments in rows:
        grouped[target_id].append(_Capture(captured_at, rate, impressions, likes, comments))
    return grouped


def _decay_ratio(captures: list[_Capture]) -> float:
    """How much of the final engagement arrived *after* the 72-hour mark.

    §8.9's `evergreen_factor`: "posts that kept earning after 72h outrank
    spike-and-die posts". A post with no capture past T+72h has no evidence
    either way and scores a neutral 1.0.
    """
    if len(captures) < 2:
        return 1.0
    cutoff = captures[0].captured_at + dt.timedelta(hours=72)
    early = [capture for capture in captures if capture.captured_at <= cutoff]
    if not early or early[-1] is captures[-1]:
        return 1.0
    baseline = early[-1].engagement_rate
    return (captures[-1].engagement_rate / baseline) if baseline else 1.0


def performance(
    workspace_id: int, *, horizon_days: int, now: dt.datetime | None = None
) -> list[TargetPerformance]:
    """Every published target inside the plan's history horizon, ranked.

    `horizon_days` is the plan's `analytics_history_days`. Bounding here rather
    than in the view is deliberate: every caller — the overview, the best-time
    model, the repurpose scan and the prompt's performance grounding — has to
    respect the same boundary, and one that each re-derived would eventually
    disagree with itself.
    """
    moment = now or timezone.now()
    since = moment - dt.timedelta(days=horizon_days)

    # `.only(...)` rather than `select_related("post")`: the loop reads the FK
    # column `post_id`, never the `Post` itself, so joining `content_post` would
    # hydrate every `master_body` for nothing.
    targets = list(
        PostTarget.objects.filter(
            post__workspace_id=workspace_id,
            state=PostTargetState.PUBLISHED,
            published_at__gte=since,
        ).only("id", "post_id", "platform", "published_at")
    )
    if not targets:
        return []

    captures = _captures_by_target(targets)

    rows: list[TargetPerformance] = []
    for target in targets:
        history = captures.get(target.pk)
        if not history or target.published_at is None:
            continue
        # The last capture is this target's current standing: a post's numbers
        # only go up, so latest-to-latest is the only comparison that treats a
        # week-old post and a month-old post fairly.
        latest = history[-1]
        rows.append(
            TargetPerformance(
                target_id=target.pk,
                post_id=target.post_id,
                platform=target.platform,
                published_at=target.published_at,
                engagement_rate=latest.engagement_rate,
                impressions=latest.impressions,
                likes=latest.likes,
                comments=latest.comments,
                decay_ratio=_decay_ratio(history),
            )
        )

    return _rank_within_trailing_window(rows, moment)


def _rank_within_trailing_window(
    rows: list[TargetPerformance], now: dt.datetime
) -> list[TargetPerformance]:
    """Percentile per platform, against the trailing 90 days only.

    Per platform because an Instagram engagement rate and a LinkedIn one are not
    the same measurement — §8.9's "cross-account comparison at different
    follower scales is meaningless" applies just as much across platforms on the
    same account.
    """
    cutoff = now - dt.timedelta(days=TRAILING_WINDOW_DAYS)
    by_platform: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if row.published_at >= cutoff:
            by_platform[row.platform].append(index)

    ranked = list(rows)
    for indexes in by_platform.values():
        if len(indexes) < MIN_HISTORY_FOR_PERCENTILE:
            continue
        # `common.ranking` returns 0-1, shared with the trend scorer so a tie is
        # broken the same way in both; percentiles are reported as 0-100.
        ranks = percentiles([rows[index].engagement_rate for index in indexes])
        for rank, index in zip(ranks, indexes, strict=True):
            ranked[index] = replace(rows[index], percentile=round(rank * 100, 2))
    return sorted(ranked, key=lambda row: row.engagement_rate, reverse=True)


def best_times(rows: list[TargetPerformance]) -> list[dict[str, Any]]:
    """Engagement bucketed by `(weekday, hour)`, best first.

    Buckets with one observation are dropped: "Tuesday 14:00 is your best slot"
    on the strength of a single post is not a finding, it is a coincidence with
    a chart.
    """
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for row in rows:
        local = row.published_at
        buckets[(local.weekday(), local.hour)].append(row.engagement_rate)

    return sorted(
        (
            {
                "weekday": weekday,
                "hour": hour,
                "samples": len(values),
                "engagement_rate": round(sum(values) / len(values), 6),
            }
            for (weekday, hour), values in buckets.items()
            if len(values) > 1
        ),
        key=lambda bucket: bucket["engagement_rate"],
        reverse=True,
    )


def _group(rows: list[TargetPerformance], key: dict[int, str]) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[key.get(row.post_id, "UNKNOWN")].append(row.engagement_rate)
    return {
        name: {
            "posts": len(values),
            "engagement_rate": round(sum(values) / len(values), 6),
        }
        for name, values in grouped.items()
    }


def attribution(
    rows: list[TargetPerformance],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Engagement by `source` and by media type (§8.9's format attribution).

    The `AI` vs `MANUAL` split is the one the user is really asking about, and
    it is reported whichever way it comes out — an honest self-audit is only
    worth having if it can say the AI is not helping.
    """
    posts = (
        Post.objects.filter(pk__in={row.post_id for row in rows})
        .only("id", "source")
        .prefetch_related("media_assets")
    )
    by_source_key: dict[int, str] = {}
    by_media_key: dict[int, str] = {}
    for post in posts:
        by_source_key[post.pk] = str(post.source or PostSource.MANUAL)
        media = list(post.media_assets.all())
        by_media_key[post.pk] = media[0].kind if media else "TEXT"
    return _group(rows, by_source_key), _group(rows, by_media_key)


def overview(workspace_id: int, *, horizon_days: int) -> Overview:
    rows = performance(workspace_id, horizon_days=horizon_days)
    if not rows:
        return Overview()

    by_source, by_media = attribution(rows)
    return Overview(
        posts=len(rows),
        impressions=sum(row.impressions for row in rows),
        engagement_rate=round(sum(row.engagement_rate for row in rows) / len(rows), 6),
        top=rows[:5],
        by_source=by_source,
        by_media=by_media,
        # Folded in rather than left to a second endpoint the page would also
        # have to call: `best_times` is a pure function of `rows`, so serving it
        # separately meant running the whole `performance()` scan twice per page
        # load. The standalone endpoint stays for API callers who want only this.
        best_times=best_times(rows),
    )


def sentiment_summary(workspace_id: int, *, horizon_days: int) -> dict[str, Any]:
    """Comment sentiment across a workspace's published posts.

    A service rather than an aggregate inlined in the view, and bounded by the
    same horizon everything else here respects — a plan that promises seven days
    of history should not have its sentiment counted over all time.
    """
    since = timezone.now() - dt.timedelta(days=horizon_days)
    totals = Comment.objects.filter(
        post_target__post__workspace_id=workspace_id, posted_at__gte=since
    ).aggregate(
        total=Count("id"),
        positive=Count("id", filter=Q(sentiment=Sentiment.POSITIVE)),
        neutral=Count("id", filter=Q(sentiment=Sentiment.NEUTRAL)),
        negative=Count("id", filter=Q(sentiment=Sentiment.NEGATIVE)),
        score=Avg("sentiment_score"),
    )
    totals["score"] = round(totals["score"] or 0.0, 4)
    return totals
