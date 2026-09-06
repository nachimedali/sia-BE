"""The provider rate budget (C-06, P0-41..P0-44).

The property this file defends is Part 7 rule 11 — **publishing is never
blocked by capture** — and its corollary, P0-G4: capture load can be doubled
with zero late publishes.

The mechanism is one bucket with a floor. Two independently-sized buckets could
jointly exceed the provider's real cap, since neither would know about the
other's draw; one bucket with a reserve only publishing may cross keeps total
throughput bounded whatever the mix.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from analytics.models import Availability, CaptureDeferral, PostMetric
from analytics.services import ingest
from analytics.tests.conftest import make_target
from common.models import RateBudgetWindow
from common.ratelimit import ProviderRateLimiter
from scheduling import publishing

pytestmark = pytest.mark.django_db


def _drain_to_reserve(account: Any) -> None:
    """Spend everything capture is allowed, leaving only publishing's floor."""
    limiter = publishing._limiter(account)
    spendable = publishing.PUBLISH_CAPACITY - int(limiter._reserve)
    limiter._bucket.consume(spendable)


# -----------------------------------------------------------------------------
# Publishing keeps its reservation — Part 7 rule 11
# -----------------------------------------------------------------------------
def test_capture_cannot_spend_publishings_reserve(social_account: Any) -> None:
    _drain_to_reserve(social_account)

    assert not publishing.capture_limiter(social_account).consume_for_background()
    assert publishing._limiter(social_account).consume_for_publish()


def test_doubling_capture_load_leaves_publishing_untouched(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """P0-G4 in miniature. Capture draws until it is refused; publishing still
    has its whole reserve, which is what "zero late publishes" means at this
    level."""
    limiter = publishing.capture_limiter(social_account)
    while limiter.consume_for_background():
        pass

    reserve = int(publishing._limiter(social_account)._reserve)
    granted = sum(
        1 for _ in range(reserve) if publishing._limiter(social_account).consume_for_publish()
    )

    assert granted == reserve


# -----------------------------------------------------------------------------
# Capture reschedules, never errors — P0-42
# -----------------------------------------------------------------------------
def test_an_exhausted_budget_defers_the_rung_rather_than_raising(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """A raised exception would pollute the failure metrics publishing alerts
    on, so a quiet budget squeeze would read as a publishing outage."""
    make_target(paid_workspace, user, social_account, age_days=0)
    _drain_to_reserve(social_account)

    assert ingest.capture_due() == 0
    assert not PostMetric.objects.exists()
    assert CaptureDeferral.objects.get().count == 1


def test_a_deferred_rung_is_still_owed_and_is_taken_when_budget_returns(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """The whole reason deferral is cheap: nothing was lost."""
    make_target(paid_workspace, user, social_account, age_days=0)
    _drain_to_reserve(social_account)
    assert ingest.capture_due() == 0

    publishing._limiter(social_account).reset()

    assert ingest.capture_due() == 1
    assert not CaptureDeferral.objects.exists()


# -----------------------------------------------------------------------------
# Three deferrals is a gap, not a silence — P0-44
# -----------------------------------------------------------------------------
def test_three_consecutive_deferrals_record_an_unavailable_gap(
    paid_workspace: Any, user: Any, social_account: Any, metrics_provider: Any
) -> None:
    """A rung deferred forever would silently shrink the sample size behind a
    confidence grade. Recording it as `UNAVAILABLE` takes it out of every
    denominator through the queryset instead."""
    make_target(paid_workspace, user, social_account, age_days=0)

    for _ in range(CaptureDeferral.MAX_DEFERRALS):
        _drain_to_reserve(social_account)
        ingest.capture_due()

    row = PostMetric.objects.get()
    assert row.availability == Availability.UNAVAILABLE
    assert row.impressions is None
    assert PostMetric.objects.analysable().count() == 0


# -----------------------------------------------------------------------------
# Redis down is conservative, never unlimited — P0-43
# -----------------------------------------------------------------------------
def test_redis_down_refuses_capture(social_account: Any, monkeypatch: Any) -> None:
    limiter = publishing.capture_limiter(social_account)

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("redis is away")

    monkeypatch.setattr(limiter._bucket, "consume", explode)

    assert not limiter.consume_for_background()


def test_redis_down_still_lets_publishing_through_but_counts_it(
    social_account: Any, monkeypatch: Any
) -> None:
    """Publishing is never blocked — but "not blocked" is not "unlimited". The
    Postgres counter is the floor underneath the bucket."""
    limiter = publishing._limiter(social_account)

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RedisConnectionError("redis is away")

    monkeypatch.setattr(limiter._bucket, "consume", explode)

    assert limiter.consume_for_publish()

    window = RateBudgetWindow.objects.get()
    assert window.consumed_publish == 1
    assert window.provider == "zernio"


def test_the_postgres_fallback_still_enforces_the_cap() -> None:
    """The point of P0-43: degraded, not disabled."""
    for _ in range(3):
        assert RateBudgetWindow.consume(provider="zernio", connection="ig:1", limit=3)

    assert not RateBudgetWindow.consume(provider="zernio", connection="ig:1", limit=3)
    # A different connection has its own allowance.
    assert RateBudgetWindow.consume(provider="zernio", connection="ig:2", limit=3)


def test_the_window_rolls_over() -> None:
    now = dt.datetime(2026, 6, 1, 9, 30, tzinfo=dt.UTC)
    later = now + dt.timedelta(hours=1)

    assert RateBudgetWindow.consume(provider="z", connection="c", limit=1, now=now)
    assert not RateBudgetWindow.consume(provider="z", connection="c", limit=1, now=now)
    assert RateBudgetWindow.consume(provider="z", connection="c", limit=1, now=later)


# -----------------------------------------------------------------------------
# The key carries the provider — P0-41
# -----------------------------------------------------------------------------
def test_the_budget_key_separates_providers() -> None:
    """Today publishing and capture share one Zernio bucket. If measurement
    ever draws a separate quota, the key already separates them — that is the
    whole reason the provider is in it before it is needed."""
    zernio = ProviderRateLimiter("instagram", 1, capacity=5, refill_per_second=1)
    other = ProviderRateLimiter(
        "instagram", 1, capacity=5, refill_per_second=1, provider="somebody-else"
    )

    assert zernio._bucket.key != other._bucket.key
    assert "zernio" in zernio._bucket.key
