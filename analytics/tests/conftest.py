"""Shared scaffolding for the analytics suite.

Everything here exists because a percentile is meaningless against one post:
nearly every assertion in this app needs a small *population* to rank within,
and four modules were otherwise writing the same three constructions by hand.
`MIN_HISTORY_FOR_PERCENTILE` is what the population sizes are chosen against, so
it is imported rather than hard-coded — a change to the floor should move these
helpers with it, not silently make every test assert nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from analytics.models import PostMetric
from analytics.services.signals import MIN_HISTORY_FOR_PERCENTILE
from content.models import Post, PostStatus, PostTarget, PostTargetState


@pytest.fixture
def published_target(paid_workspace: Any, user: Any, social_account: Any) -> Any:
    """One published copy, thirty days old — recent enough to sit inside Pro's
    90-day horizon, old enough that the ladder has rungs behind it."""
    return make_target(paid_workspace, user, social_account, age_days=30)


@pytest.fixture
def other_workspace(plans: dict[str, Any]) -> tuple[Any, Any, Any]:
    """A second paid workspace, its owner and a connected account.

    Returned as a triple because the scoping tests need all three: something to
    attribute posts to, someone to author them, and an account to publish
    through. Three modules were standing this up by hand.
    """
    from django.contrib.auth import get_user_model

    from channels.models import SocialAccount
    from workspaces.services.provisioning import provision_workspace

    user = get_user_model().objects.create_user(email="other@example.com", password="pw")
    workspace = provision_workspace(user, name="Other Studio")
    workspace.plan = plans["pro"]
    workspace.save(update_fields=["plan"])
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        handle="@other",
        provider_account_id="acct-other-1",
    )
    return workspace, user, account


def make_target(
    workspace: Any,
    user: Any,
    account: Any,
    *,
    age_days: int = 30,
    body: str = "Our seasonal glaze is live today.",
    source: str = "MANUAL",
    provider_post_id: str = "",
) -> PostTarget:
    """A published post + target pair, `age_days` in the past."""
    published = timezone.now() - dt.timedelta(days=age_days)
    post = Post.objects.create(
        workspace=workspace,
        author=user,
        master_body=body,
        status=PostStatus.PUBLISHED,
        source=source,
    )
    return PostTarget.objects.create(
        post=post,
        platform=account.platform,
        social_account=account,
        state=PostTargetState.PUBLISHED,
        published_at=published,
        provider_post_id=provider_post_id or f"zp-{post.pk}",
    )


def capture(
    target: Any, *, rate: float, at: dt.datetime | None = None, **totals: int
) -> PostMetric:
    """One metric capture on a target. `at` defaults to now."""
    return PostMetric.objects.create(
        post_target=target,
        captured_at=at or timezone.now(),
        engagement_rate=rate,
        impressions=totals.get("impressions", 1000),
        likes=totals.get("likes", 10),
        comments=totals.get("comments", 1),
    )


def make_population(
    workspace: Any,
    user: Any,
    account: Any,
    *,
    age_days: int = 30,
    winner_body: str = "Three things nobody tells you about glazing at home.",
    winner_kept_earning: bool = False,
) -> PostTarget:
    """A distribution with one clear winner, and the winner's target.

    Sized to `MIN_HISTORY_FOR_PERCENTILE` exactly: five posts make the ladder
    0/25/50/75/100, so only the winner clears the default 80 repurpose
    threshold. A sixth would put the runner-up at exactly 80 and make "one
    candidate" an accident of arithmetic rather than a rule.

    `winner_kept_earning` adds a late capture well above the T+72h one, which is
    what makes the winner read as evergreen rather than spike-and-die (§8.9).
    """
    published = timezone.now() - dt.timedelta(days=age_days)
    weak_rates = [0.01 * (n + 1) for n in range(MIN_HISTORY_FOR_PERCENTILE - 1)]
    for rate in weak_rates:
        capture(
            make_target(workspace, user, account, age_days=age_days),
            rate=rate,
            at=published + dt.timedelta(hours=72),
        )

    winner = make_target(workspace, user, account, age_days=age_days, body=winner_body)
    capture(winner, rate=0.10, at=published + dt.timedelta(hours=72))
    if winner_kept_earning:
        capture(winner, rate=0.30, at=published + dt.timedelta(days=14))
    return winner
