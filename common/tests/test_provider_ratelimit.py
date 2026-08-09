"""`ProviderRateLimiter` — publish priority over background provider traffic
(design.md §11, implementation.md Phase 6).

No real caller exists yet: `channels.SocialAccount` (Phase 9), the trend
harvester (Phase 10) and the metrics poller (Phase 11) are all unbuilt, so
these tests exercise the primitive directly — the same deferred-caller shape
as `products.services.guards.ensure_generation_ready` (design.md A54).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import time_machine

from common.ratelimit import ProviderRateLimiter


def test_publish_bucket_priority_starves_metrics_not_publishing() -> None:
    """The named Phase 6 test. Background traffic (`metrics_q`/`trends_q`)
    hammers the shared bucket concurrently; it is capped well short of
    emptying it, and publish can still draw from what background could never
    touch."""
    limiter = ProviderRateLimiter("instagram", capacity=10, refill_per_second=0.001)

    with time_machine.travel(0, tick=False):
        with ThreadPoolExecutor(max_workers=8) as pool:
            # capacity(10) - reserve(20% of 10 = 2) = 8 is all background can
            # ever take; 20 attempts is comfortable margin over that cap
            # without spending extra Redis round trips proving nothing new.
            background_allowed = sum(
                pool.map(lambda _: limiter.consume_for_background().allowed, range(20))
            )

        assert background_allowed == 8
        assert limiter.consume_for_publish().allowed


def test_publish_allow_rate_is_unaffected_by_saturated_background_traffic() -> None:
    """Regression reference for implementation.md Phase 16 ("rerun the P6
    load test; assert no regression"). Not a wall-clock measurement — that
    would be flaky under CI's variable host speed — but the deterministic
    property the reserve floor guarantees: across repeated refill cycles,
    with background traffic saturating the bucket every cycle, publish's
    allow-rate never drops below 100%."""
    limiter = ProviderRateLimiter("tiktok", capacity=20, refill_per_second=2)
    cycles = 5

    with time_machine.travel(0, tick=False) as traveller:
        publish_allowed = 0
        for _ in range(cycles):
            # capacity(20) - reserve(4) = 16 is the most background can ever
            # take in one cycle (the first, from a full bucket); 20 attempts
            # is enough margin to saturate every cycle.
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: limiter.consume_for_background(), range(20)))
            publish_allowed += int(limiter.consume_for_publish().allowed)
            traveller.shift(1)  # a fresh second of refill before the next cycle

    assert publish_allowed == cycles


def test_background_alone_can_still_drain_to_the_reserve_floor() -> None:
    """Without any publish contention, background traffic is still capped at
    the floor — the reserve is not "for publish only when publish is active",
    it is permanently off-limits to background."""
    limiter = ProviderRateLimiter("youtube", capacity=5, refill_per_second=0.001)

    with time_machine.travel(0, tick=False):
        results = [limiter.consume_for_background().allowed for _ in range(10)]

    assert sum(results) == 4  # capacity(5) - reserve(20% of 5 = 1)


def test_buckets_are_independent_per_platform_and_account() -> None:
    with time_machine.travel(0, tick=False):
        one = ProviderRateLimiter("instagram", 111, capacity=1, refill_per_second=1)
        other = ProviderRateLimiter("instagram", 222, capacity=1, refill_per_second=1)

        assert one.consume_for_publish().allowed
        assert not one.consume_for_publish().allowed
        # A different account on the same platform is unaffected.
        assert other.consume_for_publish().allowed


def test_reset_clears_both_publish_and_background_state() -> None:
    with time_machine.travel(0, tick=False):
        limiter = ProviderRateLimiter("instagram", capacity=1, refill_per_second=1)
        assert limiter.consume_for_publish().allowed
        assert not limiter.consume_for_publish().allowed

        limiter.reset()

        assert limiter.consume_for_publish().allowed


def test_social_account_id_defaults_before_channels_exists() -> None:
    """`social_account_id=None` is the shape every caller uses until Phase 9
    (A32/A47/A48/A54) — this just proves it produces a stable, usable bucket
    rather than colliding across platforms."""
    with time_machine.travel(0, tick=False):
        instagram = ProviderRateLimiter("instagram", capacity=1, refill_per_second=1)
        tiktok = ProviderRateLimiter("tiktok", capacity=1, refill_per_second=1)

        assert instagram.consume_for_publish().allowed
        assert not instagram.consume_for_publish().allowed
        assert tiktok.consume_for_publish().allowed
