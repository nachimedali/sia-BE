"""Auto-publish (design.md §8.5, I6, I9; implementation.md Phase 9).

```
Beat scan due posts -> publish_q -> entitlement preflight -> adaptation
  -> provider call -> store provider_post_id -> schedule metric polls
```

Everything except the last step is here; metric polls are Phase 11's, and
`PostTarget.provider_post_id` is what that phase will hang them on.

**Idempotency (I9) is enforced twice, on purpose.** `PostTarget.
idempotency_key` is minted once, when the target row is created, and reused on
every attempt — so the provider recognises a retry as a retry. But a provider
is not a correctness boundary: `_publish_target` also refuses to call at all
for a target already in `PUBLISHED`, so a replayed task does not depend on the
remote system remembering anything. The key covers the crash-after-send window
that local state cannot; the local check covers everything else.

**Back-off.** A retryable failure (429, 5xx — the adapter classifies, not this
module) re-raises for Celery's `autoretry_for` with an exponential countdown.
`PostTarget.attempt_count` counts consecutive failures, and the third one
alerts the workspace owner and lands the target in `FAILED` with an
`error_detail` a human can act on.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.utils import timezone

from billing.services.entitlements import entitlements_for
from billing.services.subscriptions import current_period_start
from channels import services as channel_services
from channels.adapters.base import PlatformError
from channels.adapters.zernio import get_platform_adapter
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import OCCSError
from common.mail import Email, get_mail_sender
from common.ratelimit import ProviderRateLimiter
from content.models import DeliveryMode, Post, PostStatus, PostTarget, PostTargetState
from content.services.adaptation import render_post

logger = logging.getLogger(__name__)

#: Consecutive failures on one target before it stops being retried and the
#: workspace is told (implementation.md Phase 9: "alert on 3rd failure").
MAX_ATTEMPTS = 3

#: Per-account throughput allowed to the provider. Sized against Zernio's
#: documented per-account velocity cap of 25 posts/hour (design.md §14, V1
#: finding 2) — the binding constraint in a catch-up burst, well below any
#: plan's own quota.
PUBLISH_CAPACITY = 25
PUBLISH_REFILL_PER_SECOND = 25 / 3600


class NoConnectedAccountsError(OCCSError):
    default_code = "no_connected_accounts"
    default_detail = "Connect at least one account before scheduling an auto-publish post."


def build_targets(post: Post) -> list[PostTarget]:
    """One target per publishable connected account, created when the post is
    scheduled rather than when it fires.

    Doing it at schedule time is what makes the calendar honest: the user can
    see where a post is going before it goes, and `idempotency_key` — which
    must survive every retry — is minted once here rather than by whichever
    attempt happens to run first.
    """
    accounts = list(channel_services.active_accounts(post.workspace))
    if not accounts:
        raise NoConnectedAccountsError(detail={"workspace": post.workspace_id})

    # `dict.fromkeys` rather than a plain comprehension: two accounts on one
    # platform need the payload adapted once, not once each.
    payloads = render_post(post, list(dict.fromkeys(account.platform for account in accounts)))
    targets = []
    for account in accounts:
        target, _created = PostTarget.objects.update_or_create(
            post=post,
            social_account=account,
            defaults={
                "platform": account.platform,
                "rendered_payload": payloads[account.platform].as_dict(),
                "state": PostTargetState.PENDING,
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        targets.append(target)
    return targets


def due_post_ids() -> list[int]:
    """Every auto-publish post whose slot has arrived, oldest first. Ids only:
    both callers hand them straight to `publish_post_by_id`, which loads the
    post with the relations it actually needs."""
    return list(
        Post.objects.filter(
            status=PostStatus.SCHEDULED,
            delivery_mode=DeliveryMode.AUTO_PUBLISH,
            scheduled_at__lte=timezone.now(),
        )
        .order_by("scheduled_at")
        .values_list("pk", flat=True)
    )


def _period_autopublish_count(post: Post) -> int:
    """Auto-published posts already delivered this billing period — the
    denominator `max_autopublish_posts` caps. Counted per period rather than
    per calendar month so it lines up with what was invoiced, exactly as the
    credit grant does."""
    return (
        Post.objects.filter(
            workspace=post.workspace,
            delivery_mode=DeliveryMode.AUTO_PUBLISH,
            status=PostStatus.PUBLISHED,
            updated_at__gte=current_period_start(post.workspace),
        )
        .exclude(pk=post.pk)
        .count()
    )


def preflight(post: Post) -> None:
    """The gate every publish passes, whichever way it was triggered.

    D4 lives here: Free has `auto_publish: False`, so a Free workspace cannot
    reach the provider even if a post somehow arrived in `SCHEDULED` with
    `AUTO_PUBLISH` set — which is the point of checking again rather than
    trusting the schedule endpoint that already checked.
    """
    entitlements = entitlements_for(post.workspace)
    entitlements.require_feature("auto_publish")
    entitlements.check_quota("max_autopublish_posts", _period_autopublish_count(post))


def _limiter(account: SocialAccount) -> ProviderRateLimiter:
    return ProviderRateLimiter(
        account.platform,
        account.pk,
        capacity=PUBLISH_CAPACITY,
        refill_per_second=PUBLISH_REFILL_PER_SECOND,
    )


def _record_failure(target: PostTarget, error: PlatformError) -> None:
    target.attempt_count += 1
    target.error_detail = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        **error.payload,
    }
    terminal = not error.retryable or target.attempt_count >= MAX_ATTEMPTS
    target.state = PostTargetState.FAILED if terminal else PostTargetState.PENDING
    target.save(update_fields=["attempt_count", "error_detail", "state", "updated_at"])

    if error.needs_reauth and target.social_account:
        # The adapter has told us the platform grant is gone. Nothing here will
        # work until the user reauthorises, so say so on the connections screen
        # instead of retrying into a wall.
        target.social_account.status = SocialAccountStatus.REAUTH_REQUIRED
        target.social_account.save(update_fields=["status", "updated_at"])

    if terminal:
        _alert(target)


def _alert(target: PostTarget) -> None:
    post = target.post
    get_mail_sender().send(
        Email(
            to=post.author.email,
            subject="A scheduled post could not be published",
            template="publish_failed",
            context={"post": post, "target": target, "error": target.error_detail},
        )
    )
    logger.error(
        "publish target failed terminally",
        extra={
            "post_id": post.pk,
            "target_id": target.pk,
            "platform": target.platform,
            "attempts": target.attempt_count,
        },
    )


def _terminal(target: PostTarget, message: str, detail: dict[str, Any]) -> PlatformError:
    """Records a failure this target can never recover from on its own, and
    hands back the error for the caller to raise."""
    error = PlatformError(message, detail=detail, retryable=False)
    _record_failure(target, error)
    return error


def _publish_target(target: PostTarget) -> None:
    """Publishes one target. Communicates by raising, never by returning:
    `PlatformError` with `retryable=True` means the caller should back off — the
    Celery task is what turns that into a countdown, because only it knows how
    many attempts it has already spent.
    """
    if target.state == PostTargetState.PUBLISHED:
        # I9's local half: a replayed task does not re-send, regardless of
        # whether the provider still remembers the idempotency key.
        return

    account = target.social_account
    if account is None:
        raise _terminal(
            target, "This target has no connected account to publish through.", {}
        )

    # I6, layer three. A parked or revoked account is a *terminal* failure for
    # this target and only this target — converted rather than allowed to
    # propagate, so a second target on a healthy account still gets its turn
    # and the failed one still records why.
    try:
        channel_services.ensure_publishable(account)
    except channel_services.AccountNotPublishableError as error:
        raise _terminal(target, error.message, error.payload) from error

    allowance = _limiter(account).consume_for_publish()
    if not allowance:
        raise PlatformError(
            "Local publish rate limit reached for this account.",
            retryable=True,
            retry_after=allowance.retry_after,
        )

    target.state = PostTargetState.PUBLISHING
    target.save(update_fields=["state", "updated_at"])

    try:
        result = get_platform_adapter().publish(
            platform=target.platform,
            provider_account_id=account.provider_account_id,
            payload=target.rendered_payload,
            idempotency_key=target.idempotency_key or "",
        )
    except PlatformError as error:
        _record_failure(target, error)
        raise

    target.provider_post_id = result.provider_post_id
    target.platform_post_id = result.platform_post_id
    target.published_at = timezone.now()
    target.state = PostTargetState.PUBLISHED
    target.error_detail = {}
    target.save(
        update_fields=[
            "provider_post_id",
            "platform_post_id",
            "published_at",
            "state",
            "error_detail",
            "updated_at",
        ]
    )
    logger.info(
        "published target",
        extra={
            "post_id": target.post_id,
            "target_id": target.pk,
            "platform": target.platform,
            "replay": result.was_replay,
        },
    )


def publish_post(post: Post) -> Post:
    """Publishes every target on one post, then settles the post's own status.

    A post is `PUBLISHED` when at least one target made it out and none is
    still pending. All-failed is `FAILED`; a partial success is still
    `PUBLISHED`, because it is — the failed target carries its own
    `error_detail` and the alert has already gone out. Marking the whole post
    failed would hide the copies that are live on the platforms right now.
    """
    if post.status == PostStatus.PUBLISHED:
        return post

    preflight(post)

    # `social_account__workspace` because the publish-time cap check re-derives
    # entitlements from it per target; `post__author` because a terminal failure
    # emails them. Both would otherwise be a query each, per target.
    targets = list(post.targets.select_related("social_account__workspace", "post__author"))
    if not targets:
        targets = build_targets(post)

    post.status = PostStatus.PUBLISHING
    post.save(update_fields=["status", "updated_at"])

    retryable: PlatformError | None = None
    for target in targets:
        try:
            _publish_target(target)
        except PlatformError as error:
            if error.retryable and target.state != PostTargetState.FAILED:
                retryable = error

    _settle(post, targets)
    if retryable is not None:
        raise retryable
    return post


def _settle(post: Post, targets: list[PostTarget]) -> None:
    states = {target.state for target in targets}
    if PostTargetState.PENDING in states or PostTargetState.PUBLISHING in states:
        # Something is still owed an attempt; leave the post mid-flight so the
        # retry finds it and `preflight` does not run against a post it has
        # already counted.
        return
    post.status = (
        PostStatus.PUBLISHED if PostTargetState.PUBLISHED in states else PostStatus.FAILED
    )
    post.save(update_fields=["status", "updated_at"])


def publish_post_by_id(post_id: int) -> Post:
    """Deliberately *not* wrapped in a transaction. Every target write here
    records something that has already happened on a platform we cannot roll
    back — a `PUBLISHED` target rolled back by a sibling's retryable failure
    would be re-sent on the next attempt, which is precisely the double-post
    I9 forbids. Each write commits on its own.
    """
    post = Post.objects.select_related("workspace", "author").get(pk=post_id)
    return publish_post(post)


def publish_due() -> int:
    """Publishes every due post inline, and returns how many were attempted.

    One post's retryable failure must not abandon the rest of the scan — under
    Beat each post is an independent task, so the same has to be true here or
    the on-demand path (`publish_due_posts`, A89) would stop at the first
    provider hiccup and leave later posts untouched. Nothing is swallowed
    silently: `_record_failure` has already stored the error on the target and
    alerted on a terminal one.
    """
    attempted = 0
    for post_id in due_post_ids():
        try:
            publish_post_by_id(post_id)
        except PlatformError as error:
            logger.warning(
                "due post left for a later attempt",
                extra={"post_id": post_id, "error": error.message},
            )
        attempted += 1
    return attempted
