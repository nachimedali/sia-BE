"""Token bucket behaviour (implementation.md Phase 1).

Time is driven by time-machine rather than sleeping (implementation.md §5).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import time_machine

from common.ratelimit import TokenBucket


def test_token_bucket_limits_and_refills() -> None:
    # 5 tokens, refilling at 1/second.
    with time_machine.travel(0, tick=False) as traveller:
        bucket = TokenBucket("test:limits", capacity=5, refill_per_second=1)

        for _ in range(5):
            assert bucket.consume().allowed

        # Sixth request in the same instant is refused.
        refused = bucket.consume()
        assert not refused.allowed
        assert refused.retry_after == pytest.approx(1.0, abs=0.01)

        # Half a second buys nothing: a whole token has not accrued.
        traveller.shift(0.5)
        assert not bucket.consume().allowed

        # A full second does.
        traveller.shift(0.5)
        assert bucket.consume().allowed

        # Refill is capped at capacity — an idle bucket does not accumulate
        # unlimited burst.
        traveller.shift(3600)
        for _ in range(5):
            assert bucket.consume().allowed
        assert not bucket.consume().allowed


def test_retry_after_scales_with_the_deficit() -> None:
    with time_machine.travel(0, tick=False):
        bucket = TokenBucket("test:retry", capacity=10, refill_per_second=2)
        assert bucket.consume(10).allowed

        # Wanting 4 tokens at 2/sec is a 2-second wait.
        refused = bucket.consume(4)
        assert not refused.allowed
        assert refused.retry_after == pytest.approx(2.0, abs=0.01)


def test_buckets_are_independent_per_key() -> None:
    with time_machine.travel(0, tick=False):
        first = TokenBucket("test:a", capacity=1, refill_per_second=1)
        second = TokenBucket("test:b", capacity=1, refill_per_second=1)

        assert first.consume().allowed
        assert not first.consume().allowed
        # Draining one account's bucket must not throttle another's.
        assert second.consume().allowed


def test_concurrent_consumers_cannot_overspend() -> None:
    """The check-and-consume is a Lua script precisely so this cannot race."""
    bucket = TokenBucket("test:race", capacity=20, refill_per_second=0.001)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: bucket.consume().allowed, range(60)))

    # Refill over the test's lifetime is negligible, so exactly the capacity
    # should have been granted — never more.
    assert sum(results) == 20


def test_rejects_nonsensical_configuration() -> None:
    with pytest.raises(ValueError, match="capacity must be positive"):
        TokenBucket("test:bad", capacity=0, refill_per_second=1)
    with pytest.raises(ValueError, match="refill_per_second must be positive"):
        TokenBucket("test:bad", capacity=1, refill_per_second=0)


def test_uses_wall_clock_when_now_is_not_supplied() -> None:
    bucket = TokenBucket("test:wall", capacity=1, refill_per_second=1)
    assert bucket.consume(now=time.time()).allowed
    assert not bucket.consume().allowed


# -----------------------------------------------------------------------------
# Reserve floor (implementation.md Phase 6) — the primitive ProviderRateLimiter
# is built on. Covered here at the TokenBucket level because it is a plain
# consume() argument, independent of the (platform, social_account) keying
# ProviderRateLimiter adds; see test_provider_ratelimit.py for that.
# -----------------------------------------------------------------------------
def test_reserve_of_zero_behaves_like_a_plain_bucket() -> None:
    with time_machine.travel(0, tick=False):
        bucket = TokenBucket("test:reserve-zero", capacity=3, refill_per_second=1)
        for _ in range(3):
            assert bucket.consume(reserve=0).allowed
        assert not bucket.consume(reserve=0).allowed


def test_reserve_refuses_before_the_floor_is_crossed() -> None:
    with time_machine.travel(0, tick=False):
        bucket = TokenBucket("test:reserve", capacity=10, refill_per_second=0.001)

        # A caller with a reserve of 4 may only take the 6 tokens above it.
        allowed = sum(bucket.consume(reserve=4).allowed for _ in range(20))
        assert allowed == 6

        # The 4 reserved tokens are still sitting in the bucket, untouched.
        assert bucket.consume(reserve=0).allowed


def test_reserve_retry_after_accounts_for_the_floor() -> None:
    with time_machine.travel(0, tick=False):
        bucket = TokenBucket("test:reserve-retry", capacity=10, refill_per_second=2)
        bucket.consume(6, reserve=0)  # tokens: 4

        # Wants 3 with a reserve of 4: needs tokens to reach 7, i.e. 3 more
        # than the 4 currently sitting there, at 2/sec.
        refused = bucket.consume(3, reserve=4)
        assert not refused.allowed
        assert refused.retry_after == pytest.approx(1.5, abs=0.01)
