"""Segmentation (P7-04) — published posts reduced to comparable slices.

BUILD-PLAN names seven axes: format, platform, posting hour, topic cluster,
tone, media type and length band. All seven are derived here and handed to
`statistics.analyse` as opaque `{dimension: value}` pairs, so adding an eighth
is a function in this module and nothing anywhere else.

**A dimension nobody can answer is absent, not "unknown".** A post written by
hand carries no topic or tone — those come from the candidate that generated it
— and bucketing every hand-written post into an `unknown` slice would create a
segment that reliably outperforms, because it is mostly the posts a human cared
enough to write. Absence keeps it out of the comparison entirely.

**The window is trailing, not the campaign** (P7-05). See `statistics` for why.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

from django.db.models import Max
from django.utils import timezone

from analytics.models import PostMetric
from content.models import MediaKind, Post, PostTarget, PostTargetState
from learn.services.statistics import Observation
from planning.models import CampaignItem
from taste.models import ContentCandidate

#: Posting hour is bucketed rather than kept at hour resolution: 24 buckets
#: over a trailing window puts nearly every hour under the Insufficient floor,
#: so the dimension would be present, always ungraded, and useless.
HOUR_BUCKETS: tuple[tuple[str, range], ...] = (
    ("early_morning", range(5, 9)),
    ("morning", range(9, 12)),
    ("midday", range(12, 14)),
    ("afternoon", range(14, 18)),
    ("evening", range(18, 22)),
    ("night", range(22, 24)),
)

#: Character bands for the body. Chosen to straddle the platform limits the
#: composer already enforces rather than to be round numbers.
LENGTH_BANDS: tuple[tuple[str, int], ...] = (
    ("under_80", 80),
    ("80_to_200", 200),
    ("200_to_500", 500),
    ("over_500", 10**9),
)


def resolve_zone(tz_name: str) -> dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return dt.UTC


def hour_bucket(moment: dt.datetime, zone: dt.tzinfo) -> str:
    """Local wall hour, not UTC.

    "Posts do better in the evening" is a claim about the audience's clock. A
    UTC bucket silently reads as a different part of the day for every
    workspace, and would move for all of them twice a year.
    """
    hour = moment.astimezone(zone).hour
    for name, span in HOUR_BUCKETS:
        if hour in span:
            return name
    return "overnight"


def _length_band(body: str) -> str:
    length = len(body.strip())
    for name, ceiling in LENGTH_BANDS:
        if length < ceiling:
            return name
    return LENGTH_BANDS[-1][0]


def _media_kind(kinds: list[str]) -> str:
    if not kinds:
        return "text_only"
    unique = set(kinds)
    if len(unique) > 1:
        return "mixed"
    only = unique.pop()
    if only == MediaKind.VIDEO:
        return "video"
    if len(kinds) > 1:
        return "image_set"
    return "image" if only == MediaKind.IMAGE else only.lower()


def _latest_measured(target_ids: list[int]) -> dict[int, float]:
    """The newest *measured, real* engagement rate per target, in two queries.

    `analysable()` rather than `measured()`: a fake-adapter row can carry
    `MEASURED` availability, and no fabricated row may ever reach a digest
    (rule 17). `engagement_rate__isnull=False` stays separate from
    `analysable()` — a measured row can still lack it, when the follower
    denominator itself was unavailable — so the "latest" here means the
    latest row that actually has one, not merely the latest capture.

    Two queries rather than a window function, mirroring
    `analytics.services.reporting.latest_per_target`: the ids of the latest
    rows come back first, then the rows themselves.
    """
    scoped = PostMetric.objects.analysable().filter(
        post_target_id__in=target_ids, engagement_rate__isnull=False
    )
    latest = scoped.values("post_target_id").annotate(newest=Max("captured_at"))
    keys = {(row["post_target_id"], row["newest"]) for row in latest}
    if not keys:
        return {}
    return {
        target_id: rate
        for target_id, captured_at, rate in scoped.values_list(
            "post_target_id", "captured_at", "engagement_rate"
        )
        if rate is not None and (target_id, captured_at) in keys
    }


def observations(
    workspace_id: int,
    *,
    horizon_days: int,
    timezone_name: str = "UTC",
    now: dt.datetime | None = None,
) -> list[Observation]:
    """Every published target in the trailing window, segmented.

    `horizon_days` is the plan's `analytics_history_days`, bounded by the caller
    exactly as every other analytics reader bounds it — a digest may not reach
    past what the plan retains.
    """
    moment = now or timezone.now()
    since = moment - dt.timedelta(days=horizon_days)

    targets = list(
        PostTarget.objects.filter(
            post__workspace_id=workspace_id,
            state=PostTargetState.PUBLISHED,
            published_at__gte=since,
            published_at__lte=moment,
        )
        .select_related("post")
        .only("id", "post_id", "platform", "post_format", "published_at", "post__master_body")
    )
    if not targets:
        return []

    rates = _latest_measured([target.pk for target in targets])
    post_ids = {target.post_id for target in targets}

    # Media kinds, campaign membership and generation provenance, one query
    # each rather than per target: the loop below touches every post, and three
    # queries beats three hundred.
    media: dict[int, list[str]] = {}
    # Base rows only: an override row is alt text for one platform, not a
    # second slide, and counting it would turn every post with per-platform
    # alt text into a carousel.
    for post_id, kind in Post.objects.filter(
        id__in=post_ids, media_attachments__target_override__isnull=True
    ).values_list("id", "media_attachments__media_asset__kind"):
        if kind:
            media.setdefault(post_id, []).append(kind)

    campaigns = dict(
        CampaignItem.objects.filter(post_id__in=post_ids).values_list("post_id", "campaign_id")
    )

    # **Trend stage 7 — this is where the pipeline closes** (P7-12, C-10).
    # Stage 6 wrote the cluster that justified each proposal onto the
    # candidate's payload; reading it back here is what turns "these topics
    # were trending" into "these topics actually worked for you", which is the
    # only part of the trend engine that can ever be falsified.
    #
    # The topic is read from `payload["trend"]["label"]`, the key stage 6
    # actually writes. A segmenter looking for `payload["topic"]` would have
    # found nothing, forever, and reported a working topic dimension that was
    # empty on every row — present in the schema, absent from the answer.
    provenance: dict[int, dict[str, str]] = {}
    for post_id, payload, voice in ContentCandidate.objects.filter(
        post_id__in=post_ids
    ).values_list("post_id", "payload", "taste_profile__voice"):
        dimensions: dict[str, str] = {}

        if isinstance(payload, dict):
            trend = payload.get("trend")
            if isinstance(trend, dict) and isinstance(trend.get("label"), str):
                dimensions["topic"] = trend["label"]
            elif isinstance(payload.get("topic"), str) and payload["topic"]:
                dimensions["topic"] = payload["topic"]

        # Tone is a property of the taste profile the candidate ran under, not
        # of the post — which makes "did the voice change help" answerable,
        # because profiles are versioned and every candidate records its version.
        if isinstance(voice, dict) and isinstance(voice.get("tone"), str) and voice["tone"]:
            dimensions["tone"] = voice["tone"]

        if dimensions:
            provenance[post_id] = dimensions

    zone = resolve_zone(timezone_name)
    rows: list[Observation] = []
    for target in targets:
        if target.published_at is None:
            continue
        dimensions = {
            "platform": target.platform,
            "format": target.post_format or "",
            "posting_hour": hour_bucket(target.published_at, zone),
            "media_kind": _media_kind(media.get(target.post_id, [])),
            "length_band": _length_band(target.post.master_body or ""),
            **provenance.get(target.post_id, {}),
        }
        rows.append(
            Observation(
                target_id=target.pk,
                post_id=target.post_id,
                published_at=target.published_at,
                # Absent from `rates` means unmeasured. Carried as `None` all
                # the way into the statistics rather than defaulted here.
                engagement_rate=rates.get(target.pk),
                campaign_id=campaigns.get(target.post_id),
                dimensions={key: value for key, value in dimensions.items() if value},
            )
        )

    return rows
