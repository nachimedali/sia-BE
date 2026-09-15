"""The projection (P8-02, P8-06) — tenant rows in, cohort rows out.

This module is the only part of Phase 8 that reads posts, captures and
accounts. It reduces each contributed post to a cohort key, a posting window, a
date and three rates, and writes that to `BenchmarkObservation`, which is all
the aggregator will ever see.

**Absent, never "unknown", never zero.** Every post that cannot be placed
honestly is left out rather than bucketed: no consent, no analysable capture, a
fabricated capture, still accruing, outside the window, or an account whose
size is not known. An "unknown" size band would pool an 800-follower page with
an 80,000-follower one — the exact error the band exists to prevent — and a
zero where a platform reported nothing would drag every median it touched.

**Rebuilt per contributor, not patched.** Each run deletes a workspace's rows
and writes them again in one transaction, which is what makes it idempotent
and what makes deleted posts, revoked consent and a flag switched off all fall
out of the projection by the same path.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Sequence
from typing import Any

from django.db import transaction
from django.utils import timezone

from analytics.models import AccountSnapshot
from analytics.services.reporting import latest_per_target
from benchmarks.models import BenchmarkConfig, BenchmarkObservation, ConsentRecord
from benchmarks.services import consent
from benchmarks.services.contributors import contributor_token, withdraw
from billing.services.flags import COHORT_V8, flag_enabled
from categories.models import Category
from content.models import PostTarget, PostTargetState
from learn.services.segments import hour_bucket, resolve_zone
from workspaces.models import Workspace

__all__ = ["contributor_token", "project_all", "project_workspace", "size_band", "vertical_of"]

#: How far a follower snapshot may sit from the post it sizes. Snapshots are
#: daily (01:30), so a week absorbs a worker outage without letting a count
#: from a different era of the account decide which cohort a post joins.
SNAPSHOT_TOLERANCE = dt.timedelta(days=7)


def size_band(followers: int, edges: Sequence[int]) -> str:
    """Lower-inclusive bands: `[1000, 10000]` gives `0_1000`, `1000_10000`, `10000_plus`."""
    lower = 0
    for edge in edges:
        if followers < edge:
            return f"{lower}_{edge}"
        lower = edge
    return f"{lower}_plus"


def vertical_of(category: Category) -> Category:
    """The root of the category tree. Cohorts key on the vertical, not the leaf:
    "bakeries in Portugal on Instagram at 1k-10k followers" is already narrow,
    and splitting it by sub-category would leave almost every cohort short."""
    ancestors = category.ancestors()
    return ancestors[0] if ancestors else category


def _followers_by_account(
    account_ids: set[int], *, since: dt.datetime, until: dt.datetime
) -> dict[int, tuple[list[dt.datetime], list[int]]]:
    series: dict[int, tuple[list[dt.datetime], list[int]]] = {}
    rows = (
        AccountSnapshot.objects.filter(
            social_account_id__in=account_ids,
            followers__isnull=False,
            captured_at__gte=since - SNAPSHOT_TOLERANCE,
            captured_at__lte=until,
        )
        .order_by("social_account_id", "captured_at")
        .values_list("social_account_id", "captured_at", "followers")
    )
    for account_id, captured_at, followers in rows:
        if followers is None:
            continue
        moments, counts = series.setdefault(account_id, ([], []))
        moments.append(captured_at)
        counts.append(followers)
    return series


def _nearest_followers(
    series: tuple[list[dt.datetime], list[int]] | None, moment: dt.datetime
) -> int | None:
    if series is None:
        return None
    moments, counts = series
    position = bisect.bisect_left(moments, moment)
    best: tuple[dt.timedelta, int] | None = None
    for index in (position - 1, position):
        if 0 <= index < len(moments):
            distance = abs(moments[index] - moment)
            if best is None or distance < best[0]:
                best = (distance, counts[index])
    if best is None or best[0] > SNAPSHOT_TOLERANCE:
        return None
    return best[1]


def _rates(capture: Any, followers: int) -> tuple[float | None, float | None, float | None]:
    impressions = capture.impressions
    comments = capture.comments
    reach = impressions / followers if impressions is not None and followers > 0 else None
    # A rate over zero impressions does not exist. Zero reach over a known
    # audience does, and is stored as the measured zero it is.
    comment = comments / impressions if comments is not None and impressions else None
    return capture.engagement_rate, reach, comment


def _observations(
    workspace: Workspace, category: Category, config: BenchmarkConfig, moment: dt.datetime
) -> list[BenchmarkObservation]:
    since = moment - dt.timedelta(days=config.window_days)
    until = moment - dt.timedelta(days=config.min_post_age_days)

    targets = list(
        PostTarget.objects.filter(
            post__workspace=workspace,
            state=PostTargetState.PUBLISHED,
            published_at__gte=since,
            published_at__lte=until,
            social_account__isnull=False,
        ).values_list("id", "platform", "post_format", "published_at", "social_account_id")
    )
    if not targets:
        return []

    captures = {
        capture.post_target_id: capture
        for capture in latest_per_target(workspace, starts_at=since, ends_at=moment)
        .filter(post_target_id__in=[target[0] for target in targets])
        .only("post_target_id", "engagement_rate", "impressions", "comments")
    }
    followers = _followers_by_account({target[4] for target in targets}, since=since, until=moment)

    token = contributor_token(workspace.pk)
    vertical = vertical_of(category)
    zone = resolve_zone(workspace.timezone or "UTC")

    rows: list[BenchmarkObservation] = []
    for target_id, platform, post_format, published_at, account_id in targets:
        capture = captures.get(target_id)
        if capture is None or published_at is None or not post_format:
            continue
        audience = _nearest_followers(followers.get(account_id), published_at)
        if audience is None:
            continue
        engagement, reach, comment = _rates(capture, audience)
        if engagement is None and reach is None and comment is None:
            # Counting a post that contributes no value to any metric would
            # inflate the cohort's post count toward a threshold it has not met.
            continue
        rows.append(
            BenchmarkObservation(
                contributor=token,
                vertical=vertical,
                market=workspace.market,
                platform=platform,
                size_band=size_band(audience, config.size_band_edges),
                post_format=post_format,
                posting_window=hour_bucket(published_at, zone),
                published_on=published_at.date(),
                engagement_rate=engagement,
                reach_rate=reach,
                comment_rate=comment,
            )
        )
    return rows


def project_workspace(
    workspace: Workspace,
    *,
    now: dt.datetime | None = None,
    config: BenchmarkConfig | None = None,
) -> int:
    """Rebuild this workspace's rows. Returns how many it now has."""
    moment = now or timezone.now()
    config = config or BenchmarkConfig.get_solo()

    with transaction.atomic():
        withdraw(workspace.pk)
        if not flag_enabled(workspace.organization, COHORT_V8):
            return 0
        if not consent.is_contributing(workspace):
            return 0
        category = workspace.category
        if category is None or not workspace.market:
            return 0
        rows = _observations(workspace, category, config, moment)
        BenchmarkObservation.objects.bulk_create(rows)
        return len(rows)


def project_all(*, now: dt.datetime | None = None) -> int:
    """The nightly rebuild. Returns the number of rows projected in total.

    Iterates every workspace that has *ever* recorded consent, because the ones
    that have since revoked, gone stale under new terms or had the flag turned
    off are exactly the ones whose rows must go. Anything left behind by a
    workspace that no longer exists at all is purged at the end.
    """
    moment = now or timezone.now()
    config = BenchmarkConfig.get_solo()

    kept: set[str] = set()
    total = 0
    ever_consented = ConsentRecord.objects.values_list("workspace_id", flat=True).distinct()
    workspaces = Workspace.objects.filter(pk__in=ever_consented).select_related(
        "organization", "category"
    )
    for workspace in workspaces.iterator():
        count = project_workspace(workspace, now=moment, config=config)
        if count:
            kept.add(contributor_token(workspace.pk))
            total += count

    BenchmarkObservation.objects.exclude(contributor__in=kept).delete()
    return total
