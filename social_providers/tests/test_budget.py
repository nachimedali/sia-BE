"""The team-wide budget (SA-12): publishing keeps its reserve, the vendor's
count is the authority, and a blind limiter is never unlimited.

Times are passed in (`now=`), never slept on.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from redis.exceptions import RedisError

from social_providers.socialapi.budget import (
    Priority,
    RateState,
    TeamBudget,
    parse_headers,
)

pytestmark = pytest.mark.django_db

T0 = 1_800_000_000.0


def drain(budget: TeamBudget, priority: str, now: float = T0) -> int:
    granted = 0
    while budget.acquire(priority, now=now):
        granted += 1
        if granted > 10_000:  # pragma: no cover - a runaway is a failure
            raise AssertionError("budget never ran out")
    return granted


def test_background_stops_at_the_reserve_and_publishing_does_not(
    budget: TeamBudget, settings: Any
) -> None:
    settings.SOCIALAPI_REQUESTS_PER_MINUTE = 60
    settings.SOCIALAPI_PUBLISH_RESERVE_RATIO = 0.4

    assert drain(budget, Priority.BACKGROUND) == 36  # 60 minus the 24 held for publishing
    refused = budget.acquire(Priority.BACKGROUND, now=T0)
    assert not refused and refused.retry_after > 0
    assert drain(budget, Priority.PUBLISH) == 24


def test_doubling_capture_load_never_starves_a_publish(budget: TeamBudget, settings: Any) -> None:
    """Phase 0's gate for this provider: capture hammers the budget at twice
    its share and every publish in the same minute still gets a token."""
    settings.SOCIALAPI_REQUESTS_PER_MINUTE = 60
    settings.SOCIALAPI_PUBLISH_RESERVE_RATIO = 0.4
    for _ in range(72):  # 2x the background share
        budget.acquire(Priority.BACKGROUND, now=T0)
    published = [bool(budget.acquire(Priority.PUBLISH, now=T0 + i * 0.01)) for i in range(20)]
    assert all(published)


def test_the_limit_is_learned_from_the_vendor(budget: TeamBudget, settings: Any) -> None:
    settings.SOCIALAPI_REQUESTS_PER_MINUTE = 60
    budget.observe(RateState(limit=600, remaining=590, reset=30))
    assert budget.limit() == 600
    budget.reset()
    assert budget.limit() == 60


def test_the_vendors_zero_holds_everyone_until_reset(budget: TeamBudget) -> None:
    budget.observe(RateState(limit=60, remaining=0, reset=20), now=T0)
    for priority in (Priority.PUBLISH, Priority.BACKGROUND):
        refused = budget.acquire(priority, now=T0 + 1)
        assert not refused
        assert refused.retry_after == pytest.approx(19)
    assert budget.acquire(Priority.PUBLISH, now=T0 + 21)


def test_a_429_holds_everyone(budget: TeamBudget) -> None:
    budget.observe(RateState(limit=60, remaining=None, reset=34), status=429, now=T0)
    assert not budget.acquire(Priority.PUBLISH, now=T0 + 5)


def test_inside_the_reserve_only_publishing_may_continue(budget: TeamBudget, settings: Any) -> None:
    # Another key or the dashboard spent the minute: the vendor says 10 left of
    # 60, which is inside the 24 reserved. Our bucket alone would not know.
    settings.SOCIALAPI_PUBLISH_RESERVE_RATIO = 0.4
    budget.observe(RateState(limit=60, remaining=10, reset=15), now=T0)
    assert not budget.acquire(Priority.BACKGROUND, now=T0 + 1)
    assert budget.acquire(Priority.PUBLISH, now=T0 + 1)


def test_headers_parse_and_tolerate_garbage() -> None:
    assert parse_headers(
        {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59", "X-RateLimit-Reset": "41"}
    ) == RateState(60, 59, 41.0)
    assert parse_headers({"Retry-After": "12"}) == RateState(None, None, 12.0)
    assert parse_headers({"X-RateLimit-Limit": "lots"}) == RateState()
    assert parse_headers(object()) == RateState()


def test_redis_down_keeps_publishing_and_refuses_background(budget: TeamBudget) -> None:
    with mock.patch("common.ratelimit.TokenBucket.consume", side_effect=RedisError("down")):
        assert not budget.acquire(Priority.BACKGROUND, now=T0)
        assert budget.acquire(Priority.PUBLISH, now=T0)


def test_calibration_never_raises_when_redis_is_down(budget: TeamBudget) -> None:
    with mock.patch.object(budget._redis, "set", side_effect=RedisError("down")):
        budget.observe(RateState(limit=600, remaining=0, reset=5))
