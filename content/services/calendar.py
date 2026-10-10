"""What the calendar reads and writes (`new_temp/calendar.html`).

**Placement is a proposal.** A post on the calendar before it is approved sits
at `proposed_scheduled_at`; once the schedule service has run it sits at
`scheduled_at`. `effective_at` is whichever applies, and it is the only time the
calendar asks about. Nothing in this module writes `scheduled_at` — that column
has exactly one writer (`scheduling.services.schedule_post`, behind the approval
gate), and a drag that wrote it directly would be a second, ungated path to a
live account (L-2).

Filters are applied **here, in the queryset**, because the list paginates: a
filter run in the browser over one page would silently miss every match after
it. An unrecognised value is a 400 — a typo that returns everything is worse
than one that refuses.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.db.models.lookups import GreaterThanOrEqual, IsNull, LessThan
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from accounts.models import User
from billing.services.entitlements import entitlements_for
from collaboration.models import Thread, ThreadStatus
from common.exceptions import OCCSError, StateConflict
from common.visibility import request_audience, visible_values
from content.models import (
    ContentKind,
    DeliveryMode,
    Platform,
    Post,
    PostSource,
    PostStatus,
)
from content.services.media import ingest_media
from content.services.posts import create_post, update_post
from products.models import Product
from workspaces.models import Workspace
from workspaces.services import approvals

#: A post can be moved on the calendar only while it is still in the author's
#: hands. Beyond that it is in the schedule service's, which has gates a
#: proposal does not (horizon, quota, entitlement).
PROPOSABLE = frozenset({PostStatus.DRAFT, PostStatus.CHANGES_REQUESTED, PostStatus.PENDING_REVIEW})

#: Not yet on the calendar, and still something a person could place.
UNPLACED = PROPOSABLE | {PostStatus.APPROVED}

#: How far ahead auto-schedule looks for a free slot, when the plan has no
#: horizon of its own. A search bound, not a commercial number.
SEARCH_DAYS = 56
#: The earliest a placed slot may be: a proposal for ten minutes from now is a
#: proposal nobody can review.
LEAD = dt.timedelta(hours=1)


# --- reading -----------------------------------------------------------------
def _bad(name: str, code: str, message: str) -> OCCSError:
    return OCCSError(message, code=code, detail={name: message})


def _instant(name: str, raw: str, *, end: bool) -> dt.datetime:
    """A datetime, or a bare date read as the start (or end) of that day, UTC."""
    parsed: dt.datetime | None = None
    try:
        parsed = parse_datetime(raw)
        if parsed is None:
            day = parse_date(raw)
            if day is not None:
                parsed = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
                if end:
                    parsed += dt.timedelta(days=1)
    except ValueError:
        parsed = None
    if parsed is None:
        raise _bad(name, "invalid_date", f"'{raw}' is not a date or a datetime.")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


#: When the calendar places a post: the schedule once it has one, the author's
#: proposal until then. An expression rather than only an annotation so the
#: filters below read the same definition the serializer does.
EFFECTIVE = Coalesce("scheduled_at", "proposed_scheduled_at")


def annotate(queryset: QuerySet[Post]) -> QuerySet[Post]:
    """The two derived columns the calendar filters and counts on.

    The thread count is a subquery, not a join: the search filter below also
    walks `threads`, and a count that shared that join would report only the
    threads the search matched.
    """
    open_threads = (
        Thread.objects.filter(post=OuterRef("pk"), status=ThreadStatus.OPEN)
        .order_by()
        .values("post")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return queryset.annotate(
        effective_time=EFFECTIVE,
        open_threads=Coalesce(Subquery(open_threads, output_field=IntegerField()), 0),
    )


def apply_filters(
    queryset: QuerySet[Post],
    params: Mapping[str, Any],
    *,
    request: Any = None,
    window: bool = True,
) -> QuerySet[Post]:
    """`params` is a `QueryDict`-like: `getlist` for the repeatable ones.

    `window=False` skips `from`/`to` — the summary counts the whole filtered set
    and the window separately.
    """
    getlist = getattr(params, "getlist", lambda name: [params[name]] if name in params else [])

    products = getlist("product")
    if products:
        try:
            queryset = queryset.filter(product_id__in=[int(p) for p in products])
        except ValueError as exc:
            raise _bad("product", "invalid_product", "Product ids are numbers.") from exc

    sources = getlist("source")
    if sources:
        unknown = sorted(set(sources) - set(PostSource.values))
        if unknown:
            raise _bad("source", "invalid_source", f"Unknown post source: {', '.join(unknown)}.")
        queryset = queryset.filter(source__in=sources)

    platforms = getlist("platform")
    if platforms:
        unknown = sorted(set(platforms) - set(Platform.values))
        if unknown:
            raise _bad("platform", "invalid_platform", f"Unknown platform: {', '.join(unknown)}.")
        # Where a post has targets those are the truth; before any exist, the
        # plan it was made with is all the calendar has.
        planned = Q()
        for platform in platforms:
            planned |= Q(planned_platforms__contains=[platform])
        queryset = queryset.filter(
            Q(targets__platform__in=platforms) | (Q(targets__isnull=True) & planned)
        )

    text = (params.get("q") or "").strip() if hasattr(params, "get") else ""
    if text:
        allowed = visible_values(request_audience(request)) if request is not None else None
        comment_match = Q(threads__comments__body__icontains=text)
        if allowed is not None:
            comment_match &= Q(threads__comments__visibility__in=allowed)
        queryset = queryset.filter(
            Q(master_body__icontains=text) | Q(product__name__icontains=text) | comment_match
        )

    if str(params.get("unscheduled", "")).lower() in {"1", "true"}:
        queryset = queryset.filter(
            IsNull(EFFECTIVE, True),
            status__in=UNPLACED,
            content_kind=ContentKind.SOCIAL,
        )

    if window:
        if params.get("from"):
            queryset = queryset.filter(
                GreaterThanOrEqual(EFFECTIVE, _instant("from", params["from"], end=False))
            )
        if params.get("to"):
            queryset = queryset.filter(LessThan(EFFECTIVE, _instant("to", params["to"], end=True)))

    return queryset.distinct()


def summarise(
    queryset: QuerySet[Post], params: Mapping[str, Any], *, request: Any = None
) -> dict[str, Any]:
    """Counts under the same filters the list uses, so a tile and the list it
    opens can never disagree. `in_window` is `None` unless a window was asked
    for: a tile reading 0 for "no window" would be a figure the page invented."""
    whole = apply_filters(queryset, params, request=request, window=False)
    by_status = Counter(whole.values_list("status", flat=True))
    open_threads = Thread.objects.filter(
        post__in=whole.values("pk"), status=ThreadStatus.OPEN
    ).count()
    windowed = None
    if params.get("from") or params.get("to"):
        windowed = apply_filters(queryset, params, request=request, window=True).count()
    return {
        "total": sum(by_status.values()),
        "by_status": dict(by_status),
        "open_threads": open_threads,
        "in_window": windowed,
    }


# --- placing a post ------------------------------------------------------------
def _mode(post: Post, requested: str) -> str:
    """The delivery mode a proposal carries. Resolved now rather than at
    approval so a reviewer sees what they would be agreeing to — and so a plan
    that cannot auto-publish says so (402) when the post is *placed*, not days
    later when someone approves it."""
    from scheduling.services import default_delivery_mode

    entitlements = entitlements_for(post.workspace)
    if not requested:
        return str(default_delivery_mode(post.workspace, entitlements=entitlements))
    if requested == DeliveryMode.AUTO_PUBLISH:
        entitlements.require_feature("auto_publish")
    return requested


@transaction.atomic
def propose_time(
    post: Post,
    *,
    scheduled_at: dt.datetime | None,
    delivery_mode: str = "",
    actor: User,
) -> Post:
    """Place a post on the calendar — or take it off (`scheduled_at=None`).

    Writes `proposed_*` only. `submit_for_review` leaves an existing proposal in
    place and the final approval consumes it through `schedule_post`, so the
    horizon, quota and entitlement gates all still apply — they apply when the
    post is committed to going out, which is when they should.
    """
    if post.content_kind == ContentKind.DOC:
        raise StateConflict(
            "A document has no slot on the calendar.", detail={"content_kind": post.content_kind}
        )
    if post.status not in PROPOSABLE:
        raise StateConflict(
            f"A post in {post.status} status is moved by rescheduling it, not by proposing a time.",
            detail={"post": post.pk, "status": post.status},
        )

    if scheduled_at is None:
        post.proposed_scheduled_at = None
        post.proposed_delivery_mode = ""
    else:
        post.proposed_scheduled_at = scheduled_at
        post.proposed_delivery_mode = _mode(post, delivery_mode)
    post.save(update_fields=["proposed_scheduled_at", "proposed_delivery_mode", "updated_at"])
    approvals.log(
        workspace=post.workspace,
        actor=actor,
        verb="post.time_proposed" if scheduled_at else "post.time_withdrawn",
        target_repr=f"post {post.pk}",
        meta={"scheduled_at": scheduled_at.isoformat() if scheduled_at else None},
    )
    return post


@transaction.atomic
def upload_post(
    *,
    workspace: Workspace,
    actor: User,
    upload: Any,
    caption: str,
    product: Product | None = None,
    platform: str = "",
    scheduled_at: dt.datetime | None = None,
    delivery_mode: str = "",
) -> Post:
    """A finished piece the team brought in. It lands in **review**, never on a
    schedule: bringing your own media is not a way around the approval every
    other post goes through (L-2)."""
    asset = ingest_media(workspace=workspace, upload=upload)
    post = create_post(workspace=workspace, author=actor, master_body=caption, media_assets=[asset])
    update_post(
        post,
        reason="uploaded",
        source=PostSource.UPLOAD,
        product=product,
        planned_platforms=[platform] if platform else [],
    )
    if scheduled_at is not None:
        approvals.submit_for_review(
            post,
            actor=actor,
            delivery_mode=_mode(post, delivery_mode),
            scheduled_at=scheduled_at,
        )
        post.refresh_from_db()
    return post


# --- auto-schedule ----------------------------------------------------------------
def best_slot_for(workspace: Workspace, platform: str) -> tuple[int, int] | None:
    """The `(weekday, hour)` this workspace's own published posts did best at, on
    one platform — or `None`.

    `None` is the honest answer when there is no measurement: fewer than two
    posts in any one slot (`signals.best_times` drops a bucket of one) or a
    platform that reports nothing. The calendar never substitutes a typical
    posting hour; an invented "best time" is a recommendation with no evidence.
    """
    from analytics.services import signals

    horizon = entitlements_for(workspace).analytics_horizon_days()
    rows = [
        row
        for row in signals.performance(workspace.pk, horizon_days=horizon)
        if row.platform == platform
    ]
    buckets = signals.best_times(rows)
    return (buckets[0]["weekday"], buckets[0]["hour"]) if buckets else None


def _next_slot(
    slot: tuple[int, int], *, taken: set[tuple[str, dt.date]], platform: str, until: dt.datetime
) -> dt.datetime | None:
    weekday, hour = slot
    now = timezone.now()
    day = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    for _ in range(SEARCH_DAYS + 1):
        if day.weekday() == weekday and day >= now + LEAD and (platform, day.date()) not in taken:
            return day if day <= until else None
        day += dt.timedelta(days=1)
        if day > until:
            return None
    return None


def auto_schedule(
    workspace: Workspace, *, actor: User, post_ids: Iterable[int] | None = None
) -> list[dict[str, Any]]:
    """Propose the best hour for each unplaced draft, one outcome per post.

    **Reports, never guesses.** A post with no platform, a platform with no
    measured best hour, or no free slot inside the plan's horizon comes back
    with a `reason` and is left exactly as it was. Slots are never stacked: two
    posts for one platform do not share a day.
    """
    posts = Post.objects.filter(
        workspace=workspace,
        content_kind=ContentKind.SOCIAL,
        status=PostStatus.DRAFT,
        proposed_scheduled_at__isnull=True,
        scheduled_at__isnull=True,
    ).order_by("created_at", "pk")
    if post_ids is not None:
        posts = posts.filter(pk__in=list(post_ids))

    entitlements = entitlements_for(workspace)
    horizon = entitlements.quota("scheduling_horizon_days")
    limit_days = SEARCH_DAYS if horizon in (None, -1) or horizon > SEARCH_DAYS else int(horizon)
    until = timezone.now() + dt.timedelta(days=limit_days)

    taken: set[tuple[str, dt.date]] = set()
    placed = Post.objects.filter(workspace=workspace).exclude(IsNull(EFFECTIVE, True))
    for post in placed.prefetch_related("targets"):
        when = post.scheduled_at or post.proposed_scheduled_at
        if when is None:
            continue
        names = [t.platform for t in post.targets.all()] or list(post.planned_platforms)
        taken.update((name, when.date()) for name in names)

    slots: dict[str, tuple[int, int] | None] = {}
    results: list[dict[str, Any]] = []
    for post in posts:
        platform = post.planned_platforms[0] if post.planned_platforms else ""
        if not platform:
            results.append({"post": post.pk, "scheduled_at": None, "reason": "no_platform"})
            continue
        if platform not in slots:
            slots[platform] = best_slot_for(workspace, platform)
        slot = slots[platform]
        if slot is None:
            results.append({"post": post.pk, "scheduled_at": None, "reason": "no_best_time"})
            continue
        when = _next_slot(slot, taken=taken, platform=platform, until=until)
        if when is None:
            results.append({"post": post.pk, "scheduled_at": None, "reason": "no_free_slot"})
            continue
        try:
            propose_time(post, scheduled_at=when, actor=actor)
        except (DjangoValidationError, ValidationError):  # pragma: no cover - defensive
            results.append({"post": post.pk, "scheduled_at": None, "reason": "refused"})
            continue
        taken.add((platform, when.date()))
        results.append({"post": post.pk, "scheduled_at": when.isoformat(), "reason": ""})
    return results
