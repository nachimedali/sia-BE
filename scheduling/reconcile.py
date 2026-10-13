"""After the publish: read outcomes, sweep drafts, free provider media (SA-32).

SocialAPI accepts a publish and delivers it in the background. Until its
webhooks are wired (SA-40…42) this poll is the only way an outcome arrives,
and it stays as the safety net after: a webhook can be lost, a poll cannot be
forgotten.

* **Pending targets** (`publish_requested`) are read back after a short grace.
  Published → the target is `PUBLISHED` with the platform's id and address, and
  the capture ladder picks it up from `published_at`. Failed → terminal, with
  the platform's reason, and the owner is told. Still publishing after
  `UNCONFIRMED_AFTER` → terminal `publish_unconfirmed`, because by then a human
  has to look at the platform; it is never published again from here.
* **Drafts that will never be published** — their target failed, or the post
  was taken off the calendar — are deleted at the provider. Only while nothing
  of them is live: the adapter refuses to delete anything else.
* **Orphan drafts** — a crash between the provider creating a draft and our
  journal recording it — are found on the workspace's brand and deleted once
  nothing of ours points at them. Drafts are free and never go live, so this is
  housekeeping, not safety.
* **Provider media** uploaded for a terminal target is released; media still in
  use is kept for the next run.

Only targets journalled by the provider currently selected are touched: an id
from one vendor is never sent to another.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from django.db.models import Q
from django.utils import timezone

from channels.adapters import get_platform_adapter, provider_key
from channels.adapters.base import PlatformError, PublishResult
from content.models import Post, PostStatus, PostTarget, PostTargetState, ProviderState
from scheduling import publishing
from scheduling.journal import TargetJournal

logger = logging.getLogger(__name__)

#: How long a fresh publish is left alone before the first read.
PENDING_GRACE = dt.timedelta(minutes=1)
#: After this, a publish still in progress needs a person, not another poll.
UNCONFIRMED_AFTER = dt.timedelta(hours=24)
#: How old a draft must be before it is treated as abandoned.
DRAFT_STALE_AFTER = dt.timedelta(minutes=30)
#: Rows handled per run, so one slow run cannot hold the queue.
BATCH = 200


def _each[Row, Answer](
    rows: Iterable[Row], call: Callable[[Row], Answer], failure: str
) -> Iterator[tuple[Row, Answer]]:
    """`(row, answer)` for every row whose provider call succeeded. A retryable
    error ends the run — the budget or the vendor says wait, and the next run
    carries on; any other is logged and that row skipped."""
    for row in rows:
        try:
            answer = call(row)
        except PlatformError as error:
            if error.retryable:
                return
            logger.warning(failure, extra={"row": getattr(row, "pk", row), "code": error.code})
            continue
        yield row, answer


def reconcile_pending(*, now: dt.datetime | None = None) -> int:
    now = now or timezone.now()
    targets = list(
        PostTarget.objects.filter(
            state=PostTargetState.PUBLISHING,
            provider_state=ProviderState.PUBLISH_REQUESTED,
            provider_key=provider_key("publish"),
            provider_state_at__lte=now - PENDING_GRACE,
        )
        .select_related("post__author", "post__workspace", "social_account")
        .order_by("provider_state_at")[:BATCH]
    )
    if not targets:
        return 0
    adapter = get_platform_adapter()
    settled = 0
    posts: dict[int, Post] = {}
    for target, result in _each(
        targets,
        lambda target: adapter.fetch_publish_status(provider_post_id=target.provider_post_id),
        "could not read a pending publish",
    ):
        if result.pending:
            if now - (target.provider_state_at or now) < UNCONFIRMED_AFTER:
                continue
            result = PublishResult.unconfirmed(
                target.provider_post_id,
                "The platform has not confirmed this post after a day. Check the platform "
                "before publishing it again.",
            )
        publishing.record_outcome(target, result)
        settled += 1
        posts[target.post_id] = target.post
    _settle_posts(posts.values())
    return settled


def _settle_posts(posts: Iterable[Post]) -> None:
    """Each post once, from one read of all their targets."""
    in_flight = [post for post in posts if post.status == PostStatus.PUBLISHING]
    if not in_flight:
        return
    by_post: dict[int, list[PostTarget]] = defaultdict(list)
    for target in PostTarget.objects.filter(post__in=in_flight):
        by_post[target.post_id].append(target)
    for post in in_flight:
        publishing.settle(post, by_post[post.pk])


def sweep_drafts(*, now: dt.datetime | None = None) -> int:
    """Delete our journalled drafts that will never be published."""
    now = now or timezone.now()
    abandoned = Q(state=PostTargetState.FAILED) | (
        ~Q(post__status__in=(PostStatus.SCHEDULED, PostStatus.PUBLISHING))
        & Q(provider_state_at__lte=now - DRAFT_STALE_AFTER)
    )
    targets = (
        PostTarget.objects.filter(
            abandoned,
            provider_key=provider_key("publish"),
            provider_state=ProviderState.DRAFT_CREATED,
        )
        .exclude(provider_post_id="")
        .order_by("provider_state_at")[:BATCH]
    )
    adapter = get_platform_adapter()
    swept = 0
    for target, _ in _each(
        targets,
        lambda target: adapter.discard_draft(provider_post_id=target.provider_post_id),
        "draft not discarded",
    ):
        TargetJournal(target).record(provider_state=ProviderState.DISCARDED)
        swept += 1
    return swept


def release_media() -> int:
    """Free provider-side media of targets whose publish is over."""
    targets = (
        PostTarget.objects.filter(
            provider_key=provider_key("publish"),
            provider_state__in=(
                ProviderState.PUBLISHED,
                ProviderState.FAILED,
                ProviderState.DISCARDED,
            ),
        )
        .exclude(provider_media_ids=[])
        .order_by("provider_state_at")[:BATCH]
    )
    adapter = get_platform_adapter()
    released = 0
    for target, kept in _each(
        targets,
        lambda target: adapter.release_media(media_ids=list(target.provider_media_ids or [])),
        "media not released",
    ):
        released += len(target.provider_media_ids or []) - len(kept)
        target.provider_media_ids = kept
        target.save(update_fields=["provider_media_ids", "updated_at"])
    return released


def sweep_orphan_drafts(*, now: dt.datetime | None = None) -> int:
    """Drafts on our brands that no target of ours references — left by a
    crash between the provider creating them and the journal recording them."""
    from workspaces.models import Workspace

    now = now or timezone.now()
    adapter: Any = get_platform_adapter()
    if not hasattr(adapter, "stale_drafts"):
        return 0
    profiles = (
        Workspace.objects.exclude(provider_profile_id="")
        .values_list("provider_profile_id", flat=True)
        .distinct()
    )
    swept = 0
    for _, stale in _each(
        profiles,
        lambda profile_id: adapter.stale_drafts(
            profile_id=profile_id, created_before=now - DRAFT_STALE_AFTER
        ),
        "drafts not listed",
    ):
        known = set(
            PostTarget.objects.filter(provider_post_id__in=stale).values_list(
                "provider_post_id", flat=True
            )
        )
        orphans = [pid for pid in stale if pid not in known]
        swept += sum(
            1
            for _ in _each(
                orphans,
                lambda pid: adapter.discard_draft(provider_post_id=pid),
                "orphan draft not discarded",
            )
        )
    return swept


def run(*, now: dt.datetime | None = None) -> dict[str, int]:
    """One pass of the frequent jobs, for the beat task and the command."""
    return {
        "settled": reconcile_pending(now=now),
        "drafts_discarded": sweep_drafts(now=now),
        "media_released": release_media(),
    }
