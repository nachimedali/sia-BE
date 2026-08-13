"""Metric and comment ingestion (design.md §8.9; implementation.md Phase 11).

**The ladder**: T+1h, T+6h, T+24h, T+72h, T+7d, then weekly to the plan's
analytics horizon. Dense early because that is when a post's fate is decided and
when the spike-vs-evergreen distinction (§8.9) is actually visible; sparse later
because a 40-day-old post's numbers move slowly and every capture costs a
provider call.

**Due-based, not self-scheduling.** Beat asks "which targets are owed a capture
right now?" every hour, rather than each publish queueing five future tasks. A
queued task holds state a restart loses, cannot be retuned without draining the
queue, and gets orphaned when a post is deleted — whereas the ladder computed
against `published_at` and the captures already taken is derivable at any moment
from rows that survive everything.

**Captures are immutable and unique per `(post_target, captured_at)`.** The
timestamp stored is the *rung's* nominal time, not the wall clock the worker
happened to run at: two workers racing the same rung write the same key and the
second one loses to the unique constraint, which is what makes the whole ladder
idempotent under retry.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from django.db import IntegrityError, transaction
from django.utils import timezone

from analytics.models import AccountSnapshot, Comment, PostMetric
from analytics.services.sentiment import classify_comments
from channels.adapters.base import MetricSnapshot, PlatformError
from channels.adapters.zernio import get_platform_adapter
from channels.models import SocialAccount, SocialAccountStatus
from common.ranking import INTERACTION_WEIGHTS
from content.models import PostTarget, PostTargetState

logger = logging.getLogger(__name__)

#: design.md's ladder, as offsets from `published_at`.
LADDER_HOURS: tuple[float, ...] = (1, 6, 24, 72, 24 * 7)

#: After the last fixed rung, weekly to the horizon.
WEEKLY_INTERVAL = dt.timedelta(days=7)

#: How far either side of a rung's nominal time a capture still counts as that
#: rung. Wide enough that an hourly Beat tick never skips one, narrow enough
#: that two rungs cannot collapse into each other.
RUNG_TOLERANCE = dt.timedelta(minutes=90)

#: The longest history any plan keeps (§4.1: Advanced's 730 days). Bounds the
#: scan so a post published three years ago is not re-examined every hour for
#: the rest of the deployment's life just to conclude it is owed nothing.
MAX_HORIZON_DAYS = 730


def rungs(published_at: dt.datetime, horizon_days: int) -> list[dt.datetime]:
    """Every capture time this target is owed over its whole life.

    Computed rather than stored: the plan's horizon can change (an upgrade
    lengthens it, a downgrade shortens it) and the ladder has to follow without
    a migration or a queue rewrite.
    """
    end = published_at + dt.timedelta(days=horizon_days)
    schedule = [published_at + dt.timedelta(hours=hours) for hours in LADDER_HOURS]

    moment = schedule[-1] + WEEKLY_INTERVAL
    while moment <= end:
        schedule.append(moment)
        moment += WEEKLY_INTERVAL
    return [rung for rung in schedule if rung <= end]


def due_rung(
    target: PostTarget,
    horizon_days: int,
    *,
    now: dt.datetime,
    taken: set[dt.datetime] | None = None,
) -> dt.datetime | None:
    """The rung this target is owed right now, or `None` if it is up to date.

    The *oldest* unmet rung, not the nearest: a worker that was down for a day
    should backfill T+6h before T+24h, so the decay curve keeps its shape rather
    than acquiring a hole.

    `taken` is the set of captures already held. The scan passes it in from one
    batch query for the whole tick; a caller that omits it pays a query here.
    """
    if target.published_at is None:
        return None
    if taken is None:
        taken = set(target.metrics.values_list("captured_at", flat=True))

    for rung in rungs(target.published_at, horizon_days):
        if rung > now + RUNG_TOLERANCE:
            break
        if rung not in taken:
            return rung
    return None


def engagement_rate(snapshot: MetricSnapshot, *, followers: int) -> float:
    """Weighted interactions ÷ impressions, or ÷ followers where the platform
    reports no impressions (§8.9).

    Zero when neither denominator is available — an honest "we do not know",
    rather than a number that would be ranked against posts where we do.
    """
    weighted = sum(
        weight * getattr(snapshot, field) for field, weight in INTERACTION_WEIGHTS.items()
    )
    denominator = snapshot.impressions or followers
    return weighted / denominator if denominator else 0.0


def _taken_rungs(targets: list[PostTarget]) -> dict[int, set[dt.datetime]]:
    """Which captures each target already holds, in one query for the batch.

    One query rather than one per target: the hourly scan examines every
    published post, and the overwhelming majority of them are not due.
    """
    taken: dict[int, set[dt.datetime]] = defaultdict(set)
    for target_id, captured_at in PostMetric.objects.filter(
        post_target__in=targets
    ).values_list("post_target_id", "captured_at"):
        taken[target_id].add(captured_at)
    return taken


def capture_target(target: PostTarget, rung: dt.datetime) -> PostMetric | None:
    """One rung, for one target. Returns `None` when the capture was already
    taken — which is a race, not a failure."""
    account = target.social_account
    if account is None or not target.provider_post_id:
        return None

    try:
        snapshot = get_platform_adapter().fetch_metrics(
            platform=target.platform, provider_post_id=target.provider_post_id
        )
    except PlatformError:
        # `metrics_q` is below publishing in priority and the last capture stays
        # valid — a provider hiccup means this rung is retried on the next tick,
        # not that the ladder breaks.
        logger.warning(
            "metric capture failed", exc_info=True, extra={"target_id": target.pk}
        )
        return None

    try:
        # The savepoint is load-bearing, not decoration: catching an
        # `IntegrityError` without one leaves the surrounding transaction
        # unusable, so a caller that wrapped a batch of captures would lose all
        # of them to one lost race.
        with transaction.atomic():
            return PostMetric.objects.create(
                post_target=target,
                captured_at=rung,
                impressions=snapshot.impressions,
                likes=snapshot.likes,
                comments=snapshot.comments,
                shares=snapshot.shares,
                clicks=snapshot.clicks,
                saves=snapshot.saves,
                engagement_rate=engagement_rate(snapshot, followers=account.followers_cached),
                raw=snapshot.raw,
            )
    except IntegrityError:
        # Another worker took this rung between the due check and the insert.
        # The unique constraint is the arbiter, exactly as intended.
        return None


def capture_comments(target: PostTarget) -> int:
    """New comments on one target, classified and stored.

    Watermarked on the newest comment already held, so a post with a thousand
    comments does not re-classify all of them on every capture — sentiment is a
    provider call per batch, and the stored classification never changes because
    the comment never changes.
    """
    if not target.provider_post_id:
        return 0

    newest = target.post_comments.order_by("-posted_at").values_list("posted_at", flat=True).first()
    try:
        fetched = get_platform_adapter().fetch_comments(
            platform=target.platform, provider_post_id=target.provider_post_id, since=newest
        )
    except PlatformError:
        logger.warning(
            "comment fetch failed", exc_info=True, extra={"target_id": target.pk}
        )
        return 0

    # Bounded by what was just fetched, not by the post's lifetime comment
    # count: a post with ten thousand comments would otherwise transfer ten
    # thousand ids on each of its hundred-odd captures.
    known = set(
        target.post_comments.filter(
            external_id__in=[snapshot.external_id for snapshot in fetched]
        ).values_list("external_id", flat=True)
    )
    fresh = [snapshot for snapshot in fetched if snapshot.external_id not in known]
    if not fresh:
        return 0

    classified = classify_comments([snapshot.body for snapshot in fresh])
    Comment.objects.bulk_create(
        [
            Comment(
                post_target=target,
                external_id=snapshot.external_id,
                author=snapshot.author,
                body=snapshot.body,
                sentiment=verdict.sentiment,
                sentiment_score=verdict.score,
                posted_at=snapshot.posted_at or timezone.now(),
            )
            for snapshot, verdict in zip(fresh, classified, strict=True)
        ],
        ignore_conflicts=True,
    )
    return len(fresh)


def capture_due() -> int:
    """Beat's hourly scan. Returns how many captures were taken.

    Three deliberately batched lookups, because this runs every hour over every
    published post and almost none of them are due on any given tick:

    * the target list is bounded by the longest plan horizon, so posts past
      every plan's window drop out instead of being re-examined forever;
    * the plan horizon is resolved **once per workspace**, not once per target
      (`Entitlements` is Redis-cached, but the `Plan` row behind the cache key
      still has to be read from Postgres before the cache can be consulted);
    * the captures already held come from one query for the whole batch.
    """
    from billing.services.entitlements import entitlements_for

    moment = timezone.now()
    targets = list(
        PostTarget.objects.filter(
            state=PostTargetState.PUBLISHED,
            published_at__isnull=False,
            published_at__gte=moment - dt.timedelta(days=MAX_HORIZON_DAYS),
        )
        .exclude(provider_post_id="")
        .select_related("social_account", "post__workspace__plan")
    )
    if not targets:
        return 0

    horizons: dict[int, int] = {}
    taken = _taken_rungs(targets)

    captured = 0
    for target in targets:
        workspace = target.post.workspace
        if workspace.pk not in horizons:
            horizons[workspace.pk] = entitlements_for(workspace).analytics_horizon_days()

        rung = due_rung(
            target, horizons[workspace.pk], now=moment, taken=taken[target.pk]
        )
        if rung is None:
            continue
        if capture_target(target, rung) is not None:
            captured += 1
            capture_comments(target)

    if captured:
        logger.info("captured post metrics", extra={"count": captured})
    return captured


def snapshot_accounts() -> int:
    """Daily follower counts, for the denominator `engagement_rate` falls back
    to and for the account-growth line on `/app/analytics`."""
    moment = timezone.now().replace(minute=0, second=0, microsecond=0)
    taken = 0
    for account in SocialAccount.objects.filter(status=SocialAccountStatus.ACTIVE):
        try:
            stats = get_platform_adapter().fetch_account_stats(
                provider_account_id=account.provider_account_id
            )
        except PlatformError:
            logger.warning(
                "account snapshot failed", exc_info=True, extra={"account_id": account.pk}
            )
            continue

        _snapshot, created = AccountSnapshot.objects.get_or_create(
            social_account=account,
            captured_at=moment,
            defaults={
                "followers": stats.followers,
                "following": stats.following,
                "total_posts": stats.total_posts,
            },
        )
        if created:
            # Kept on the account so `engagement_rate` has a denominator
            # without joining the snapshot table on every capture.
            account.followers_cached = stats.followers
            account.save(update_fields=["followers_cached", "updated_at"])
            taken += 1
    return taken
