"""Repurposing (design.md §8.9; implementation.md Phase 11).

> `published_at < now-60d` AND `percentile >= threshold` (default 80,
> admin-tunable) AND no reissue in 90 days. `score = percentile·0.6 +
> evergreen_factor·0.4`, where `evergreen_factor` is the engagement decay slope
> — posts that kept earning after 72h outrank spike-and-die posts.

Three eligibility rules and one score, all of them from that paragraph. The
threshold is a row rather than a constant because §8.9 says "admin-tunable",
and it sits with the other engineering-quality dial (`QualityGateConfig`) rather
than in `Plan`: I8 governs what a customer's money buys, and this is a tuning
knob for how eager the queue is.

**Refresh in place, do not stack.** The scan runs nightly over the same corpus,
so a post that qualified yesterday qualifies again today. Re-scoring the open
candidate keeps the queue one row per post — the partial unique constraint on
`RepurposeCandidate` is what makes that a guarantee rather than a convention.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from analytics.models import RepurposeCandidate, RepurposeConfig, RepurposeReason
from analytics.services import signals
from content.models import Post, PostStatus
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

#: §8.9's three eligibility rules.
#:
#: Note the band these two create with `signals.TRAILING_WINDOW_DAYS`: a post
#: has to be older than 60 days to qualify, and the percentile it is judged on
#: is computed over the trailing 90, so **only posts aged 60-90 days are ever
#: candidates**. That is the intended reading of §8.9 — a post whose percentile
#: can no longer be computed has no evidence behind it — but it is worth
#: knowing that widening one of these without the other closes the window.
MIN_AGE_DAYS = 60
NO_REISSUE_WITHIN_DAYS = 90

#: §8.9's weights: `score = percentile·0.6 + evergreen_factor·0.4`.
PERCENTILE_WEIGHT = 0.6
EVERGREEN_WEIGHT = 0.4

#: A decay ratio at or above this counts as "kept earning" rather than
#: "spiked and died" — the difference between the two `RepurposeReason`s.
EVERGREEN_RATIO = 1.05


def _evergreen_factor(decay_ratio: float) -> float:
    """The decay slope, normalised into 0-100 so it is commensurate with the
    percentile it is averaged against.

    A post holding its T+72h engagement scores 50; one that doubled scores 100;
    one that collapsed scores near zero. Without this the two terms in §8.9's
    formula would be on different scales and the 0.6/0.4 weighting would mean
    nothing.
    """
    return max(0.0, min(decay_ratio * 50.0, 100.0))


def scan_workspace(
    workspace: Workspace, *, horizon_days: int, threshold: float, now: dt.datetime
) -> int:
    """Surfaces (or re-scores) every qualifying post in one workspace.

    `threshold` is passed in rather than read here: it is a singleton whose value
    cannot change mid-scan, so reading it per workspace would be a query per
    workspace for the same answer.
    """
    rows = signals.performance(workspace.pk, horizon_days=horizon_days, now=now)
    # Best copy per post: a post published to three platforms is one candidate,
    # judged on the platform where it did best rather than three times.
    best: dict[int, signals.TargetPerformance] = {}
    for row in rows:
        current = best.get(row.post_id)
        if current is None or row.engagement_rate > current.engagement_rate:
            best[row.post_id] = row

    # One query for the whole workspace rather than an EXISTS per candidate.
    recently_reissued = _recently_reissued(set(best), now)

    surfaced = 0
    for post_id, row in best.items():
        if row.percentile is None or row.percentile < threshold:
            continue
        if row.published_at > now - dt.timedelta(days=MIN_AGE_DAYS):
            continue
        if post_id in recently_reissued:
            continue

        evergreen = _evergreen_factor(row.decay_ratio)
        RepurposeCandidate.objects.update_or_create(
            post_id=post_id,
            dismissed_at__isnull=True,
            reissued_post__isnull=True,
            defaults={
                "percentile": row.percentile,
                "reason": (
                    RepurposeReason.EVERGREEN
                    if row.decay_ratio >= EVERGREEN_RATIO
                    else RepurposeReason.SPIKE_STEADY
                ),
                "score": round(
                    row.percentile * PERCENTILE_WEIGHT + evergreen * EVERGREEN_WEIGHT, 4
                ),
                "surfaced_at": now,
                "published_at": row.published_at,
            },
        )
        surfaced += 1
    return surfaced


def _recently_reissued(post_ids: set[int], now: dt.datetime) -> set[int]:
    """§8.9's "no reissue in 90 days" — measured from when the reissue was
    *created*, so a post run again last month is not offered again this one."""
    return set(
        RepurposeCandidate.objects.filter(
            post_id__in=post_ids,
            reissued_post__isnull=False,
            updated_at__gte=now - dt.timedelta(days=NO_REISSUE_WITHIN_DAYS),
        ).values_list("post_id", flat=True)
    )


def scan_all() -> int:
    """The nightly scan (§8.9: "Repurposing (nightly, `trends_q`)").

    Repurposing is a paid feature, so a Free workspace is skipped here rather
    than having candidates built it could never open — the entitlement is the
    same `repurposing` flag the endpoint checks.
    """
    from billing.services.entitlements import entitlements_for

    now = timezone.now()
    threshold = RepurposeConfig.get_solo().percentile_threshold
    total = 0
    for workspace in Workspace.objects.filter(posts__status=PostStatus.PUBLISHED).distinct():
        entitlements = entitlements_for(workspace)
        if not entitlements.feature("repurposing"):
            continue
        horizon = entitlements.analytics_horizon_days()
        if horizon < MIN_AGE_DAYS:
            # A plan whose history is shorter than the minimum age can never
            # see a qualifying post: by the time one is old enough, it has
            # fallen out of the window the percentile is computed over.
            continue
        total += scan_workspace(workspace, horizon_days=horizon, threshold=threshold, now=now)

    if total:
        logger.info("surfaced repurpose candidates", extra={"count": total})
    return total


def dismiss(candidate: RepurposeCandidate) -> RepurposeCandidate:
    candidate.dismissed_at = timezone.now()
    candidate.save(update_fields=["dismissed_at", "updated_at"])
    return candidate


def accept(candidate: RepurposeCandidate, *, author_id: int) -> Post:
    """Opens the reissue as a fresh draft carrying its origin.

    The draft is created here and the *generation* is Phase 12/14's job to run
    against it — §8.9's "accepting opens a `REPURPOSE` generation carrying
    `origin_post`, capped at 0.8 similarity to the original" needs a generation
    mode that exists (`GenerationMode.REPURPOSE` does) and a similarity cap the
    quality gate enforces, which is where that check already lives. What this
    function owns is the link: a post that knows what it came from.
    """
    origin = candidate.post
    reissue = Post.objects.create(
        workspace=origin.workspace,
        author_id=author_id,
        master_body=origin.master_body,
        status=PostStatus.DRAFT,
        source=origin.source,
        category=origin.category,
        product=origin.product,
    )
    reissue.media_assets.set(origin.media_assets.all())

    candidate.reissued_post = reissue
    candidate.save(update_fields=["reissued_post", "updated_at"])
    return reissue
