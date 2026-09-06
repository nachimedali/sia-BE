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
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from analytics.models import (
    AccountSnapshot,
    AudienceComment,
    Availability,
    PostMetric,
    ProviderCursor,
)
from analytics.providers import get_metrics_registry
from analytics.providers.base import MetricsError, MetricsUnsupportedError, RawMetricPayload
from analytics.services.normalise import normalise
from analytics.services.sentiment import classify_comments
from channels.models import SocialAccount, SocialAccountStatus
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

#: **The ladder stops here** (U-4, P0-40). The built ladder ran weekly to the
#: plan horizon with no stop, so an Advanced workspace kept paying a provider
#: call per post per week for two years to watch numbers that had not moved
#: since month one. Thirty days covers the whole of the decay curve §8.9
#: actually reads; beyond it a post is settled.
#:
#: A stop, not a shorter horizon: `analytics_history_days` still governs how
#: long the *history* is readable. This governs how long we keep asking.
CAPTURE_STOP_DAYS = 30


def rungs(published_at: dt.datetime, horizon_days: int) -> list[dt.datetime]:
    """Every capture time this target is owed over its whole life.

    Computed rather than stored: the plan's horizon can change (an upgrade
    lengthens it, a downgrade shortens it) and the ladder has to follow without
    a migration or a queue rewrite.
    """
    # Whichever comes first: the plan's horizon, or the point past which a
    # post's numbers have stopped moving (P0-40).
    end = published_at + dt.timedelta(days=min(horizon_days, CAPTURE_STOP_DAYS))
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


#: The `external_id` of the marker row that records "this platform exposes no
#: comment endpoint" (P0-03). A sentinel row rather than a flag on `PostTarget`
#: because `AudienceComment.availability` is where BUILD-PLAN puts the answer,
#: and because it keeps "unavailable" and "empty" in the same place the reader
#: is already looking. `AudienceComment.objects.measured()` filters it out, so
#: no denominator can ever count it as a comment.
UNAVAILABLE_MARKER = "__comments_unavailable__"


def _taken_rungs(targets: list[PostTarget]) -> dict[int, set[dt.datetime]]:
    """Which captures each target already holds, in one query for the batch.

    One query rather than one per target: the hourly scan examines every
    published post, and the overwhelming majority of them are not due.
    """
    taken: dict[int, set[dt.datetime]] = defaultdict(set)
    for target_id, captured_at in PostMetric.objects.filter(post_target__in=targets).values_list(
        "post_target_id", "captured_at"
    ):
        taken[target_id].add(captured_at)
    return taken


def capture_target(target: PostTarget, rung: dt.datetime) -> PostMetric | None:
    """One rung, for one target. Returns `None` when nothing was written —
    which is a race, a deferral or a sync still in flight, never a failure
    worth a zero row.

    Three outcomes that used to be one:

    * **measured** — a row with numbers, and nulls for whatever the provider
      did not report;
    * **unavailable** — a row with *no* numbers, recording that we asked and
      this platform does not answer. Excluded from every denominator by
      `PostMetric.objects.analysable()`;
    * **pending** — nothing written at all. Zernio's `202` means its own sync
      has not finished, so the rung stays unmet and the next tick retries it.
      Writing a row now would make a permanent statement about a moment
      nobody measured (P0-32).
    """
    account = target.social_account
    if account is None or not target.provider_post_id:
        return None

    registry = get_metrics_registry()
    provider = registry.for_platform(target.platform)
    if provider is None:
        # Nobody covers this platform. That is an answer — unavailable — and
        # A-19 says it is stored as one rather than as six zeros.
        return _write(target, rung, _uncovered(target), provider=None, followers=None)

    try:
        payload = provider.fetch(platform=target.platform, provider_post_id=target.provider_post_id)
    except MetricsError:
        # `metrics_q` is below publishing in priority and the last capture
        # stays valid — a provider hiccup means this rung is retried on the
        # next tick, not that the ladder breaks.
        logger.warning("metric capture failed", exc_info=True, extra={"target_id": target.pk})
        return None

    if payload.is_pending:
        logger.info("metric capture pending", extra={"target_id": target.pk})
        return None

    return _write(
        target,
        rung,
        payload,
        provider=provider,
        followers=account.followers_cached or None,
        reactions=_post_reactions(provider, target),
    )


def _post_reactions(provider: Any, target: PostTarget) -> dict[str, int] | None:
    """The post's reaction breakdown, or `None` where the platform does not
    give one. Zernio breaks reactions down for Facebook and LinkedIn
    organisation pages only; everywhere else the honest answer is `None`, not
    a dict of zeros per reaction type nobody reported."""
    fetch = getattr(provider, "fetch_reactions", None)
    if fetch is None:
        return None
    try:
        breakdown: dict[str, int] | None = fetch(
            platform=target.platform, provider_post_id=target.provider_post_id
        )
    except MetricsError:
        return None
    return breakdown


def _uncovered(target: PostTarget) -> RawMetricPayload:
    return RawMetricPayload(
        provider_key="",
        platform=target.platform,
        provider_post_id=target.provider_post_id,
        schema_version=0,
        availability=Availability.UNAVAILABLE,
        fetched_at=timezone.now(),
    )


def _write(
    target: PostTarget,
    rung: dt.datetime,
    payload: RawMetricPayload,
    *,
    provider: Any,
    followers: int | None,
    reactions: dict[str, int] | None = None,
) -> PostMetric | None:
    normalised = normalise(payload, provider=provider, followers=followers, reactions=reactions)
    try:
        # The savepoint is load-bearing, not decoration: catching an
        # `IntegrityError` without one leaves the surrounding transaction
        # unusable, so a caller that wrapped a batch of captures would lose all
        # of them to one lost race.
        with transaction.atomic():
            return PostMetric.objects.create(
                post_target=target,
                captured_at=rung,
                **normalised.as_model_fields(),
            )
    except IntegrityError:
        # Another worker took this rung between the due check and the insert.
        # The unique constraint is the arbiter, exactly as intended.
        return None


def capture_comments(target: PostTarget) -> int:
    """New audience comments on one target, classified and stored.

    Watermarked on the newest comment already held, so a post with a thousand
    comments does not re-classify all of them on every capture — sentiment is a
    provider call per batch, and the stored classification never changes because
    the comment never changes.

    **A platform with no comment endpoint writes an unavailability marker, not
    nothing** (P0-03). TikTok is the case: silence there is our vendor's
    coverage, not the audience's opinion, and a comment-rate that counted it as
    zero would be measuring the wrong thing.
    """
    if not target.provider_post_id:
        return 0

    provider = get_metrics_registry().for_comments(target.platform)
    if provider is None:
        _mark_comments_unavailable(target)
        return 0

    newest = (
        target.post_comments.measured()
        .order_by("-posted_at")
        .values_list("posted_at", flat=True)
        .first()
    )
    try:
        fetched = provider.fetch_comments(
            platform=target.platform, provider_post_id=target.provider_post_id, since=newest
        )
    except MetricsUnsupportedError:
        _mark_comments_unavailable(target)
        return 0
    except MetricsError:
        logger.warning("comment fetch failed", exc_info=True, extra={"target_id": target.pk})
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
    AudienceComment.objects.bulk_create(
        [
            AudienceComment(
                post_target=target,
                external_id=snapshot.external_id,
                author=snapshot.author,
                body=snapshot.body,
                sentiment=verdict.sentiment,
                sentiment_score=verdict.score,
                posted_at=snapshot.posted_at or timezone.now(),
                reactions=snapshot.reactions or {},
                availability=Availability.MEASURED,
            )
            for snapshot, verdict in zip(fresh, classified, strict=True)
        ],
        ignore_conflicts=True,
    )
    return len(fresh)


def _mark_comments_unavailable(target: PostTarget) -> None:
    """One sentinel row per target, idempotent. Says "we asked; this platform
    does not answer" so the surface can render unavailable rather than an
    empty thread that reads as "nobody said anything"."""
    AudienceComment.objects.get_or_create(
        post_target=target,
        external_id=UNAVAILABLE_MARKER,
        defaults={
            "availability": Availability.UNAVAILABLE,
            "posted_at": timezone.now(),
        },
    )


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

        rung = due_rung(target, horizons[workspace.pk], now=moment, taken=taken[target.pk])
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
    to and for the account-growth line on `/app/analytics`.

    Reads through the metrics port, not the publish adapter: a follower count
    is measurement, and after P0-30 the publish adapter has no method that
    returns one.
    """
    moment = timezone.now().replace(minute=0, second=0, microsecond=0)
    registry = get_metrics_registry()
    taken = 0
    for account in SocialAccount.objects.filter(status=SocialAccountStatus.ACTIVE):
        provider = registry.for_platform(account.platform)
        fetch = getattr(provider, "fetch_account_stats", None)
        if fetch is None:
            continue
        try:
            stats = fetch(provider_account_id=account.provider_account_id)
        except MetricsError:
            logger.warning(
                "account snapshot failed", exc_info=True, extra={"account_id": account.pk}
            )
            continue

        followers = stats.get("followers")
        _snapshot, created = AccountSnapshot.objects.get_or_create(
            social_account=account,
            captured_at=moment,
            defaults={
                "followers": followers,
                "following": stats.get("following"),
                "total_posts": stats.get("total_posts"),
            },
        )
        if created:
            # Kept on the account so `engagement_rate` has a denominator
            # without joining the snapshot table on every capture. Left
            # untouched when the provider did not report one — a stale real
            # number beats a fresh zero as a divisor.
            if followers is not None:
                account.followers_cached = followers
                account.save(update_fields=["followers_cached", "updated_at"])
            taken += 1
    return taken


def follow_delta() -> int:
    """The cheap capture path (P0-31).

    One call returns every snapshot that moved across every account, instead of
    one call per due post — by the vendor's own measurement, 1,599 calls/hour
    down to 205 over ~1,600 accounts.

    **A delta arrival satisfies a due rung; it does not create a new one.** The
    ladder still decides *when* a post is measured, because the whole analytics
    layer is built on captures at nominal rung times and unique on
    `(post_target, captured_at)`. The feed changes the transport, not the
    schedule — which is what keeps it a drop-in cost reduction rather than a
    reshaping of every downstream comparison.

    Refuses to advance until the ladder has bootstrapped this provider: the
    feed is a rolling seven-day log, so following it first would silently skip
    every post older than the window and never come back for them.
    """
    from billing.services.entitlements import entitlements_for

    registry = get_metrics_registry()
    provider = next(
        (p for p in registry.all() if hasattr(p, "changed_since")),
        None,
    )
    if provider is None:
        return 0

    state, _ = ProviderCursor.objects.get_or_create(provider_key=provider.key)
    if state.bootstrapped_at is None:
        logger.info("delta feed not bootstrapped yet", extra={"provider": provider.key})
        return 0

    try:
        payloads, next_cursor = provider.changed_since(state.cursor)
    except MetricsError:
        logger.warning("delta feed read failed", exc_info=True, extra={"provider": provider.key})
        return 0

    moment = timezone.now()
    by_provider_id = {
        target.provider_post_id: target
        for target in PostTarget.objects.filter(
            state=PostTargetState.PUBLISHED,
            provider_post_id__in=[p.provider_post_id for p in payloads if p.provider_post_id],
        ).select_related("social_account", "post__workspace__plan")
    }

    written = 0
    horizons: dict[int, int] = {}
    for payload in payloads:
        target = by_provider_id.get(payload.provider_post_id)
        if target is None:
            continue
        workspace = target.post.workspace
        if workspace.pk not in horizons:
            horizons[workspace.pk] = entitlements_for(workspace).analytics_horizon_days()

        rung = due_rung(target, horizons[workspace.pk], now=moment)
        if rung is None:
            # Nothing owed. The feed reports every movement; the ladder decides
            # which of them we keep.
            continue
        account = target.social_account
        row = _write(
            target,
            rung,
            payload,
            provider=provider,
            followers=(account.followers_cached or None) if account else None,
        )
        if row is not None:
            written += 1

    state.cursor = next_cursor
    state.empty_reads = 0 if payloads else state.empty_reads + 1
    state.save(update_fields=["cursor", "empty_reads", "updated_at"])

    if written:
        logger.info("captured metrics from delta feed", extra={"count": written})
    return written


def mark_bootstrapped(provider_key: str) -> None:
    """Called once the ladder has taken a first capture for every live target.

    Separate from `follow_delta` so the ordering is explicit rather than
    implied: bootstrap, then follow. The alternative — the follower deciding
    for itself that enough history exists — is the kind of guess that goes
    wrong quietly.
    """
    ProviderCursor.objects.update_or_create(
        provider_key=provider_key,
        defaults={"bootstrapped_at": timezone.now()},
    )
