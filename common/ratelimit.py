"""Redis token buckets (design.md §11).

Two uses:
  * `ProviderRateLimiter` — outbound buckets per (platform, social_account),
    where publishing holds priority and metrics/trend polling yield to it;
  * `TokenBucket` directly — per-IP buckets on auth endpoints, to blunt
    credential stuffing.

The refill maths runs inside a Lua script so check-and-consume is atomic — two
workers cannot both spend the last token. `now` is passed in from Python rather
than read from Redis's clock, which keeps the whole thing controllable by
time-machine in tests (implementation.md §5: never sleep).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.conf import settings
from redis.exceptions import RedisError

from common.redis import get_redis

logger = logging.getLogger(__name__)

# KEYS[1] = bucket key
# ARGV = capacity, refill_per_second, now, requested, ttl, reserve
_CONSUME_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local reserve = tonumber(ARGV[6])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

-- `reserve` carves off a floor the caller may not dip into. A caller passing
-- 0 (the default) sees ordinary token-bucket behaviour; Phase 6 uses a
-- nonzero reserve for background traffic sharing a bucket with publishing
-- (design.md §11 — see ProviderRateLimiter below).
local allowed = 0
if tokens - reserve >= requested then
  tokens = tokens - requested
  allowed = 1
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, ttl)

-- Returned as strings: Redis truncates Lua numbers to integers on the wire.
return {allowed, tostring(tokens)}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: float
    retry_after: float

    def __bool__(self) -> bool:
        return self.allowed


class TokenBucket:
    """A refilling bucket of `capacity` tokens at `refill_per_second`."""

    def __init__(
        self,
        key: str,
        capacity: int,
        refill_per_second: float,
        *,
        redis_url: str | None = None,
        namespace: str = "rl",
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")

        self.key = f"{namespace}:{key}"
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._redis = get_redis(redis_url or settings.RATELIMIT_REDIS_URL)
        self._script = self._redis.register_script(_CONSUME_LUA)
        # Idle buckets expire once they would have refilled completely; keeping
        # them longer just holds memory for a bucket that is full anyway.
        self._ttl = max(60, int(capacity / refill_per_second) * 2)

    def consume(
        self, tokens: float = 1, *, now: float | None = None, reserve: float = 0
    ) -> RateLimitResult:
        """Requests `tokens`. `reserve` is a floor this call will not dip
        below — the request is refused if granting it would leave fewer than
        `reserve` tokens, even though the bucket itself has enough. Default 0
        is a plain token bucket; `ProviderRateLimiter` is what passes a
        nonzero reserve in practice."""
        now = time.time() if now is None else now
        allowed, remaining_raw = self._script(
            keys=[self.key],
            args=[self.capacity, self.refill_per_second, now, tokens, self._ttl, reserve],
        )
        remaining = float(remaining_raw)

        retry_after = 0.0
        if not allowed:
            retry_after = max(0.0, (tokens + reserve - remaining) / self.refill_per_second)

        return RateLimitResult(allowed=bool(allowed), remaining=remaining, retry_after=retry_after)

    def reset(self) -> None:
        self._redis.delete(self.key)


class ProviderRateLimiter:
    """One shared bucket per outbound provider dependency (design.md §11,
    implementation.md Phase 6).

    Publishing draws the bucket down to zero; metrics polling and trend
    harvesting stop once a draw would eat into the reserve, so a scheduled
    09:00 post can never be delayed by a concurrent trend crawl hitting the
    same platform. One bucket, not two independently-sized ones: two buckets
    could jointly exceed the provider's real limit, since neither would know
    about the other's draw. A single bucket with a floor only publishing may
    cross keeps total throughput bounded to `capacity`/`refill_per_second`
    regardless of the mix.

    Keyed on `(provider, platform, connection)` (P0-41). The provider
    dimension is what makes the ledger survive measurement moving to a second
    vendor: today publishing and capture draw one Zernio bucket, and if they
    ever draw separate quotas the key already separates them without a
    redesign. `social_account_id` is the connection.

    **Redis down does not mean unlimited** (P0-43). A failed bucket call falls
    through to a conservative Postgres counter: publishing keeps its
    allowance, capture is refused outright. Refusing background work while
    the limiter is blind is the safe direction — a deferred capture costs
    nothing, and an unbounded one can exhaust the quota a publish needs.
    """

    #: Fraction of capacity background traffic may never draw into. design.md
    #: §11 names the shape ("publishing holds priority... yield to it") but
    #: not a ratio — one had to be picked; see design.md §15.7 (Phase 6).
    PUBLISH_RESERVE_RATIO = 0.2

    def __init__(
        self,
        platform: str,
        social_account_id: int | None = None,
        *,
        capacity: int,
        refill_per_second: float,
        provider: str = "zernio",
    ) -> None:
        account = social_account_id if social_account_id is not None else "-"
        self.provider = provider
        self.platform = platform
        self.connection = str(account)
        self._bucket = TokenBucket(
            f"provider:{provider}:{platform}:{account}", capacity, refill_per_second
        )

    @property
    def _reserve(self) -> float:
        return self._bucket.capacity * self.PUBLISH_RESERVE_RATIO

    def consume_for_publish(
        self, tokens: float = 1, *, now: float | None = None
    ) -> RateLimitResult:
        """Highest priority (design.md §5.1: `publish_q`). May spend the
        reserve — nothing is held back from it.

        **Publishing is never blocked by capture** (Part 7 rule 11), which is
        why this is also the branch that keeps working when Redis does not.
        """
        try:
            return self._bucket.consume(tokens, now=now)
        except RedisError:
            logger.warning("rate budget degraded to Postgres", extra={"provider": self.provider})
            return self._degraded_publish(tokens)

    def consume_for_background(
        self, tokens: float = 1, *, now: float | None = None
    ) -> RateLimitResult:
        """Metrics polling and trend harvesting (design.md §5.1: `metrics_q`,
        `trends_q`). Refused once granting it would cross into the reserve,
        leaving that floor untouched for publish.

        A refusal is not an error. The caller reschedules — see
        `analytics.services.ingest`, where an exception here would pollute the
        failure metrics publishing alerts on.
        """
        try:
            return self._bucket.consume(tokens, now=now, reserve=self._reserve)
        except RedisError:
            # Blind, so conservative: never unlimited (P0-43). Publishing
            # keeps its allowance above; background work waits for Redis.
            logger.warning(
                "rate budget degraded to Postgres, refusing background draw",
                extra={"provider": self.provider, "platform": self.platform},
            )
            return RateLimitResult(allowed=False, remaining=0.0, retry_after=60.0)

    def _degraded_publish(self, tokens: float) -> RateLimitResult:
        """The Postgres fallback. A coarse per-window counter — no refill
        curve, no reserve — because its only job is to keep publishing inside
        the provider's cap while the real limiter is unreachable."""
        from common.models import RateBudgetWindow

        consumed = RateBudgetWindow.consume(
            provider=self.provider,
            connection=f"{self.platform}:{self.connection}",
            limit=self._bucket.capacity,
            tokens=tokens,
        )
        return RateLimitResult(
            allowed=consumed,
            remaining=0.0,
            retry_after=0.0 if consumed else 60.0,
        )

    def reset(self) -> None:
        self._bucket.reset()
