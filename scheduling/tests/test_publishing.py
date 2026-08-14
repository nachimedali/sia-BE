"""Auto-publish: the paid delivery path (design.md §8.5, D4, I6, I9;
implementation.md Phase 9).

Every test here calls `scheduling.publishing` or the Celery task directly
rather than through a broker (A4) — the task bodies are one call each, and the
back-off decision is the only logic that lives in the task, so that is the one
thing driven through `.apply()`.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO
from typing import Any

import pytest
import time_machine
from celery.exceptions import Retry
from django.core.management import call_command
from django.utils import timezone

from channels.adapters.base import PlatformError
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import FeatureNotAvailable
from content.models import Post, PostStatus, PostTarget, PostTargetState
from content.services.posts import create_post
from scheduling import publishing, tasks
from scheduling.services import schedule_post
from workspaces.models import Membership
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


def _scheduled_post(workspace: Any, user: Any, *, body: str = "New drop is live") -> Post:
    post = create_post(workspace=workspace, author=user, master_body=body)
    schedule_post(
        post=post,
        delivery_mode="AUTO_PUBLISH",
        scheduled_at=timezone.now() + dt.timedelta(minutes=2),
    )
    return post


def _make_due(post: Post) -> Post:
    Post.objects.filter(pk=post.pk).update(scheduled_at=timezone.now() - dt.timedelta(seconds=1))
    post.refresh_from_db()
    return post


# -----------------------------------------------------------------------------
# Scheduling arms the targets
# -----------------------------------------------------------------------------
def test_scheduling_auto_publish_builds_one_target_per_connected_account(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = _scheduled_post(paid_workspace, user)

    target = PostTarget.objects.get(post=post)
    assert target.social_account_id == social_account.pk
    assert target.state == PostTargetState.PENDING
    assert target.idempotency_key, "the key is minted at schedule time, not publish time (I9)"


def test_scheduling_auto_publish_without_a_connected_account_is_refused(
    paid_workspace: Any, user: Any
) -> None:
    """Better to say so now than to accept a schedule that can only fail at
    09:00 tomorrow with nobody watching."""
    post = create_post(workspace=paid_workspace, author=user, master_body="Nowhere to go")

    with pytest.raises(publishing.NoConnectedAccountsError):
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=2),
        )


def test_rendered_payload_is_what_preview_would_have_returned(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """design.md §8.6: "the preview output must be byte-identical to what
    publish sends". One call path, asserted by comparing the JSON both
    produce, not by trusting that they share a function."""
    post = _scheduled_post(paid_workspace, user, body="Handmade in Lisbon #ceramics")

    preview = auth_client.post(
        "/api/v1/posts/preview/",
        {"master_body": post.master_body, "platforms": ["instagram"]},
        format="json",
    )

    assert preview.status_code == 200
    assert (
        PostTarget.objects.get(post=post).rendered_payload
        == (preview.json()["payloads"]["instagram"])
    )


# -----------------------------------------------------------------------------
# The happy path and the Beat scan
# -----------------------------------------------------------------------------
def test_due_post_publishes_and_stores_the_provider_post_id(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    post = _make_due(_scheduled_post(paid_workspace, user))

    assert tasks.publish_due_posts() == 1

    post.refresh_from_db()
    target = PostTarget.objects.get(post=post)
    assert post.status == PostStatus.PUBLISHED
    assert target.state == PostTargetState.PUBLISHED
    assert target.provider_post_id == "fake-post-1"
    assert target.platform_post_id == "instagram-1"
    assert target.published_at is not None
    assert len(platform_adapter.published) == 1


def test_the_scan_ignores_posts_that_are_not_yet_due(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    with time_machine.travel("2026-08-11 09:00:00+00:00", tick=False) as traveller:
        _scheduled_post(paid_workspace, user)
        assert tasks.publish_due_posts() == 0
        assert platform_adapter.published == []

        traveller.shift(dt.timedelta(minutes=2, seconds=1))
        assert tasks.publish_due_posts() == 1


def test_the_scan_ignores_reminder_mode_posts(
    workspace: Any, user: Any, platform_adapter: Any
) -> None:
    """Free's delivery path is Phase 8's, and it must never touch the
    provider — that is the whole of D4's cost argument."""
    post = create_post(workspace=workspace, author=user, master_body="Remind me")
    schedule_post(
        post=post, delivery_mode="REMINDER", scheduled_at=timezone.now() - dt.timedelta(minutes=1)
    )

    assert tasks.publish_due_posts() == 0
    assert platform_adapter.published == []


# -----------------------------------------------------------------------------
# test_retried_publish_task_does_not_double_post (I9)
# -----------------------------------------------------------------------------
def test_retried_publish_task_does_not_double_post(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Replay the task and assert one provider call and one
    `provider_post_id` (implementation.md Phase 9)."""
    post = _make_due(_scheduled_post(paid_workspace, user))

    publishing.publish_post_by_id(post.pk)
    publishing.publish_post_by_id(post.pk)
    publishing.publish_post_by_id(post.pk)

    assert len(platform_adapter.published) == 1
    target = PostTarget.objects.get(post=post)
    assert target.provider_post_id == "fake-post-1"


def test_replay_after_local_state_is_lost_still_does_not_double_post(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """The half local state cannot cover: the worker died between the
    provider accepting the post and the row being written. The next attempt
    re-sends the same `idempotency_key`, and the provider recognises it.
    """
    post = _make_due(_scheduled_post(paid_workspace, user))
    publishing.publish_post_by_id(post.pk)

    PostTarget.objects.filter(post=post).update(
        state=PostTargetState.PENDING, provider_post_id="", published_at=None
    )
    Post.objects.filter(pk=post.pk).update(status=PostStatus.SCHEDULED)

    publishing.publish_post_by_id(post.pk)

    assert len(platform_adapter.published) == 1, "the provider was asked to publish twice"
    target = PostTarget.objects.get(post=post)
    assert target.provider_post_id == "fake-post-1"
    assert target.state == PostTargetState.PUBLISHED


# -----------------------------------------------------------------------------
# test_free_tier_cannot_auto_publish (D4)
# -----------------------------------------------------------------------------
def test_free_tier_cannot_auto_publish(
    paid_workspace: Any, user: Any, social_account: Any, plans: Any, platform_adapter: Any
) -> None:
    """Checked at publish time with the schedule endpoint's own gate bypassed
    — a post that reached `SCHEDULED` any other way (a downgrade after
    scheduling, an admin edit) must still not reach the provider.

    Scheduled while paid, then downgraded: that is the realistic route to a
    Free workspace holding a `SCHEDULED` auto-publish post, and the one the
    schedule endpoint's own 402 cannot possibly catch.
    """
    post = _make_due(_scheduled_post(paid_workspace, user, body="Not on Free"))
    paid_workspace.plan = plans["free"]
    paid_workspace.save(update_fields=["plan"])
    post.refresh_from_db()

    with pytest.raises(FeatureNotAvailable):
        publishing.publish_post(post)

    assert platform_adapter.published == []


def test_auto_publish_quota_is_enforced_per_period(
    paid_workspace: Any, user: Any, social_account: Any, plans: Any, platform_adapter: Any
) -> None:
    plans["pro"].max_autopublish_posts = 1
    plans["pro"].save(update_fields=["max_autopublish_posts", "updated_at"])

    first = _make_due(_scheduled_post(paid_workspace, user, body="One"))
    publishing.publish_post_by_id(first.pk)

    second = _make_due(_scheduled_post(paid_workspace, user, body="Two"))
    with pytest.raises(Exception, match="allows 1"):
        publishing.publish_post_by_id(second.pk)

    assert len(platform_adapter.published) == 1


# -----------------------------------------------------------------------------
# test_simulated_429_backs_off_and_retries
# -----------------------------------------------------------------------------
def test_simulated_429_backs_off_and_retries(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """A 429 must schedule another attempt, not hammer and not give up. The
    countdown comes from the provider's own `Retry-After` when it sent one —
    it knows when the window reopens and we would only be guessing.
    """
    post = _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=1, retry_after=30)

    with pytest.raises(Retry) as caught:
        tasks.publish_post.apply(args=(post.pk,), throw=True).get()

    # Celery's own Retry, carrying the countdown the task asked for — so this
    # asserts the real retry path, not a stand-in for it.
    assert caught.value.when == 30.0
    # Nothing was published, and the target is still eligible for the retry.
    assert platform_adapter.published == []
    assert PostTarget.objects.get(post=post).state == PostTargetState.PENDING


def test_the_second_attempt_after_a_429_succeeds(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    post = _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=1, retry_after=30)

    with pytest.raises(PlatformError):
        publishing.publish_post_by_id(post.pk)
    publishing.publish_post_by_id(post.pk)

    post.refresh_from_db()
    assert post.status == PostStatus.PUBLISHED
    assert len(platform_adapter.published) == 1


def test_back_off_doubles_when_the_provider_names_no_window() -> None:
    error = PlatformError("nope", retryable=True)

    assert tasks._countdown(0, error) == 60
    assert tasks._countdown(1, error) == 120
    assert tasks._countdown(10, error) == tasks.RETRY_BACKOFF_MAX_SECONDS


# -----------------------------------------------------------------------------
# test_third_consecutive_failure_alerts_and_marks_failed
# -----------------------------------------------------------------------------
def test_third_consecutive_failure_alerts_and_marks_failed(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any, outbox: list[Any]
) -> None:
    post = _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=3, retryable=True)

    # The first two ask for another attempt; the third stops asking — that is
    # what "alert on the 3rd failure" means, and re-raising there would have
    # the task queue a fourth attempt nobody wants.
    for _attempt in range(2):
        with pytest.raises(PlatformError):
            publishing.publish_post_by_id(post.pk)
    publishing.publish_post_by_id(post.pk)

    target = PostTarget.objects.get(post=post)
    post.refresh_from_db()
    assert target.attempt_count == 3
    assert target.state == PostTargetState.FAILED
    assert post.status == PostStatus.FAILED
    assert target.error_detail["message"] == "Upstream failure."
    assert len(outbox) == 1
    assert outbox[0].to == user.email
    assert outbox[0].template == "publish_failed"


def test_a_terminal_failure_is_not_retried_at_all(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any, outbox: list[Any]
) -> None:
    """A rejected caption will not become acceptable on the next attempt, so
    the first failure is the last one."""
    post = _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=1, retryable=False)

    tasks.publish_post.apply(args=(post.pk,), throw=True).get()

    target = PostTarget.objects.get(post=post)
    assert target.attempt_count == 1
    assert target.state == PostTargetState.FAILED
    assert len(outbox) == 1


def test_a_disconnected_account_is_marked_for_reauth(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Retrying into a revoked platform grant is pure noise; the connections
    screen is where this belongs."""
    post = _make_due(_scheduled_post(paid_workspace, user))
    # `needs_reauth` rather than the provider's own error code: the adapter is
    # what translates one into the other, so a test that hand-crafted Zernio's
    # string here would pass even if the adapter stopped setting the flag.
    platform_adapter.queue_failure(times=1, retryable=False, needs_reauth=True, message="gone")

    publishing.publish_post_by_id(post.pk)

    social_account.refresh_from_db()
    assert social_account.status == SocialAccountStatus.REAUTH_REQUIRED


def test_publishing_through_a_parked_account_is_refused(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    post = _make_due(_scheduled_post(paid_workspace, user))
    social_account.status = SocialAccountStatus.OVER_LIMIT
    social_account.save(update_fields=["status"])

    publishing.publish_post_by_id(post.pk)

    assert platform_adapter.published == []
    assert PostTarget.objects.get(post=post).state == PostTargetState.FAILED


# -----------------------------------------------------------------------------
# The on-demand command (the Beat-role gap A89 named, third instance)
# -----------------------------------------------------------------------------
def test_publish_due_posts_command_publishes_what_beat_would_have(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    _make_due(_scheduled_post(paid_workspace, user))

    out = StringIO()
    call_command("publish_due_posts", stdout=out)

    assert "Published 1 due post(s)." in out.getvalue()
    assert len(platform_adapter.published) == 1


def test_one_posts_retryable_failure_does_not_abandon_the_rest_of_the_scan(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Under Beat each post is an independent task, so a provider hiccup on the
    first one must not stop the second from being attempted."""
    _make_due(_scheduled_post(paid_workspace, user))
    _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=1, retry_after=30)

    assert publishing.publish_due() == 2
    assert len(platform_adapter.published) == 1


def test_a_target_whose_account_was_deleted_fails_terminally(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """`PostTarget.social_account` is `SET_NULL`, so deleting an account
    leaves scheduled targets pointing at nothing. That is a dead end, not
    something a retry fixes."""
    post = _make_due(_scheduled_post(paid_workspace, user))
    social_account.delete()

    publishing.publish_post_by_id(post.pk)

    post.refresh_from_db()
    target = PostTarget.objects.get(post=post)
    assert target.state == PostTargetState.FAILED
    assert post.status == PostStatus.FAILED
    assert platform_adapter.published == []


def test_the_local_rate_limiter_defers_rather_than_hammering(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Sized against the provider's own 25-posts-per-hour per-account velocity
    cap (design.md §14, V1) — reaching it locally means backing off before
    the provider has to say no."""
    post = _make_due(_scheduled_post(paid_workspace, user))
    publishing._limiter(social_account)._bucket.consume(publishing.PUBLISH_CAPACITY)

    with pytest.raises(PlatformError, match="Local publish rate limit"):
        publishing.publish_post_by_id(post.pk)

    assert platform_adapter.published == []
    # Still PENDING, and `attempt_count` untouched: nothing was attempted, so
    # a deferral must not spend one of the three attempts a real failure gets.
    target = PostTarget.objects.get(post=post)
    assert target.state == PostTargetState.PENDING
    assert target.attempt_count == 0


def test_a_post_with_a_still_pending_target_stays_mid_flight(
    paid_workspace: Any, user: Any, social_account: Any, platform_adapter: Any
) -> None:
    """Two accounts, one failing retryably: the post must not settle as
    published or failed while an attempt is still owed."""
    SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="threads",
        provider_account_id="acct-threads-1",
        handle="@acme",
    )
    post = _make_due(_scheduled_post(paid_workspace, user))
    platform_adapter.queue_failure(times=1, retry_after=30)

    with pytest.raises(PlatformError):
        publishing.publish_post_by_id(post.pk)

    post.refresh_from_db()
    assert post.status == PostStatus.PUBLISHING
    assert {t.state for t in post.targets.all()} == {
        PostTargetState.PUBLISHED,
        PostTargetState.PENDING,
    }


# -----------------------------------------------------------------------------
# test_role_revoked_after_scheduling_blocks_publish (design.md §8.8, I5;
# implementation.md Phase 13)
# -----------------------------------------------------------------------------
def test_role_revoked_after_scheduling_blocks_publish(
    advanced_workspace: Any,
    contributor_user: Any,
    admin_user: Any,
    advanced_social_account: Any,
    platform_adapter: Any,
    outbox: Any,
) -> None:
    """The approver's role is checked again at publish time, days after
    `schedule_post` accepted it — an ADMIN removed from the workspace between
    the two must not have their approval still count.
    """
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="New drop"
    )
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = _make_due(
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
        )
    )

    Membership.objects.filter(user=admin_user, workspace=advanced_workspace).delete()

    result = publishing.publish_post_by_id(post.pk)

    assert result.status == PostStatus.FAILED
    assert platform_adapter.published == []
    assert PostTarget.objects.filter(post=post, state=PostTargetState.PUBLISHED).count() == 0
    assert len(outbox) == 1
    assert outbox[0].to == contributor_user.email


def test_role_revoked_recheck_is_not_retried(
    advanced_workspace: Any,
    contributor_user: Any,
    admin_user: Any,
    advanced_social_account: Any,
    platform_adapter: Any,
) -> None:
    """No target was touched, so there is no back-off to wait out — only a
    human decision (re-approve, reschedule) can move this forward, and Beat
    must not keep re-trying it every minute in the meantime."""
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="New drop"
    )
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = _make_due(
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
        )
    )
    Membership.objects.filter(user=admin_user, workspace=advanced_workspace).delete()

    try:
        tasks.publish_post.apply(args=(post.pk,), throw=True).get()
    except Retry:
        pytest.fail("a revoked approval must fail the post outright, not schedule a retry")

    post.refresh_from_db()
    assert post.status == PostStatus.FAILED


def test_a_demoted_but_still_admin_approver_still_counts(
    advanced_workspace: Any,
    contributor_user: Any,
    admin_user: Any,
    advanced_social_account: Any,
    platform_adapter: Any,
) -> None:
    """The check is "still ADMIN or better", not "still holds the exact same
    role" — an OWNER approving, then being (impossibly) demoted to ADMIN,
    would still be senior enough. Modelled here the more reachable way: an
    ADMIN stays an ADMIN, and publish proceeds."""
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="New drop"
    )
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = _make_due(
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
        )
    )

    result = publishing.publish_post_by_id(post.pk)

    assert result.status == PostStatus.PUBLISHED
    assert len(platform_adapter.published) == 1
