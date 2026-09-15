"""Phase 8 fixtures, registered as a plugin by the root conftest.

Two ways to put data in front of the benchmark code, and each test picks the
one that matches the layer under test:

* `publish_measured` builds the real thing — post, target, capture, follower
  snapshot — for tests of the projection, which is the only code allowed to
  read those tables.
* `observe` writes a projection row directly, for tests of the aggregator,
  which by design cannot see anything else. Building 200 real posts to test a
  median would test the ORM, slowly.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth import get_user_model

from analytics.models import AccountSnapshot, Availability, MetricSource, PostMetric
from benchmarks.models import BenchmarkConfig, BenchmarkObservation, ConsentPolicy
from billing.models import FeatureFlag
from billing.services.flags import COHORT_V8
from categories.models import Category
from channels.models import SocialAccount
from content.models import PostStatus, PostTarget, PostTargetState
from content.services.posts import create_post
from workspaces.services.provisioning import provision_workspace

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)

_emails = itertools.count(1)


@pytest.fixture
def vertical(db: None) -> Category:
    """A root category. Its child is what a workspace usually picks."""
    return Category.objects.create(name="Food & Drink", slug="food-drink")


@pytest.fixture
def leaf(vertical: Category) -> Category:
    return Category.objects.create(name="Bakeries", slug="bakeries", parent=vertical)


@pytest.fixture
def policy(db: None) -> ConsentPolicy:
    return ConsentPolicy.objects.create(
        version=1,
        summary="Aggregate statistics only. Revoking stops future contribution.",
        document_url="https://example.com/benchmark-terms/v1",
    )


@pytest.fixture
def cohort_on(db: None) -> None:
    """The rollout flag, on for everyone. It ships off (P8-01 is legal-gated)."""
    FeatureFlag.objects.create(organization=None, key=COHORT_V8, enabled=True)


@pytest.fixture
def small_thresholds(db: None) -> BenchmarkConfig:
    """Thresholds a test can satisfy with real rows, at the database's floor."""
    config = BenchmarkConfig.get_solo()
    config.min_workspaces = 3
    config.min_posts = 3
    config.save()
    return config


@pytest.fixture
def api_client_for() -> Callable[[Any, Any], Any]:
    """An APIClient acting as `user` inside `workspace` specifically."""
    from rest_framework.test import APIClient

    def _client(user: Any, workspace: Any) -> Any:
        api = APIClient()
        api.force_authenticate(user)
        api.credentials(HTTP_X_WORKSPACE_ID=str(workspace.pk))
        return api

    return _client


@pytest.fixture
def make_workspace(plans: dict[str, Any]) -> Callable[..., Any]:
    """A workspace in its own organization, on Pro, with a vertical and market."""

    def _make(
        *,
        category: Category | None = None,
        market: str = "PT",
        name: str | None = None,
        timezone: str = "UTC",
    ) -> Any:
        number = next(_emails)
        user = get_user_model().objects.create_user(
            email=f"brand{number}@example.com", password="pw"
        )
        workspace = provision_workspace(user, name=name or f"Brand {number}")
        workspace.organization.plan = plans["pro"]
        workspace.organization.save(update_fields=["plan"])
        workspace.category = category
        workspace.market = market
        workspace.timezone = timezone
        workspace.save(update_fields=["category", "market", "timezone"])
        return workspace

    return _make


def connect(workspace: Any, *, platform: str = "instagram", followers: int | None = 5000) -> Any:
    """A connected account with a follower series across the whole window, or
    with no snapshots at all when `followers` is None.

    A series rather than one snapshot: sizing uses the snapshot nearest the
    post, within a week, and a single snapshot would silently exclude every
    post more than a week from it — a fixture failure that reads as a rule.
    """
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        handle=f"@{workspace.slug}-{platform}",
        provider_account_id=f"acct-{workspace.pk}-{platform}",
    )
    if followers is not None:
        AccountSnapshot.objects.bulk_create(
            AccountSnapshot(
                social_account=account,
                captured_at=NOW - dt.timedelta(days=days_ago),
                followers=followers,
            )
            for days_ago in range(0, 220, 5)
        )
    return account


def publish_measured(
    workspace: Any,
    account: Any,
    *,
    days_ago: int = 20,
    engagement_rate: float | None = 0.05,
    impressions: int | None = 1000,
    comments: int | None = 10,
    post_format: str = "FEED",
    source: str = MetricSource.PROVIDER,
    availability: str = Availability.MEASURED,
    body: str = "A caption nobody outside this workspace should ever read.",
) -> PostTarget:
    post = create_post(workspace=workspace, author=workspace.organization.owner, master_body=body)
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])

    published_at = NOW - dt.timedelta(days=days_ago)
    target = PostTarget.objects.create(
        post=post,
        platform=account.platform,
        post_format=post_format,
        social_account=account,
        state=PostTargetState.PUBLISHED,
        published_at=published_at,
    )
    PostMetric.objects.create(
        post_target=target,
        captured_at=published_at + dt.timedelta(days=7),
        engagement_rate=engagement_rate,
        impressions=impressions,
        comments=comments,
        availability=availability,
        source=source,
        provider_key="zernio" if source == MetricSource.PROVIDER else "fake",
    )
    return target


@pytest.fixture
def observe(db: None) -> Callable[..., None]:
    """Write projection rows directly, for aggregator tests."""

    def _observe(
        *,
        vertical: Category,
        contributors: int,
        posts_each: int,
        engagement_rate: float | Callable[[int, int], float | None] = 0.05,
        reach_rate: float | None = 0.2,
        comment_rate: float | None = 0.01,
        market: str = "PT",
        platform: str = "instagram",
        size_band: str = "1000_10000",
        post_format: str = "FEED",
        posting_window: str = "evening",
        days_ago: int = 20,
        contributor_prefix: str = "c",
    ) -> None:
        rows = []
        for contributor in range(contributors):
            for post in range(posts_each):
                rate = (
                    engagement_rate(contributor, post)
                    if callable(engagement_rate)
                    else engagement_rate
                )
                rows.append(
                    BenchmarkObservation(
                        contributor=f"{contributor_prefix}{contributor}",
                        vertical=vertical,
                        market=market,
                        platform=platform,
                        size_band=size_band,
                        post_format=post_format,
                        posting_window=posting_window,
                        published_on=(NOW - dt.timedelta(days=days_ago + post % 5)).date(),
                        engagement_rate=rate,
                        reach_rate=reach_rate,
                        comment_rate=comment_rate,
                    )
                )
        BenchmarkObservation.objects.bulk_create(rows)

    return _observe
