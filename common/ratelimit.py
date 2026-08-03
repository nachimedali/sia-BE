"""Redis token buckets (design.md §11).

Two uses:
  * outbound buckets per (platform, social_account), where publishing holds
    priority and metrics/trend polling yield to it;
  * per-IP buckets on auth endpoints, to blunt credential stuffing.

The refill maths runs inside a Lua script so check-and-consume is atomic — two
workers cannot both spend the last token. `now` is passed in from Python rather
than read from Redis's clock, which keeps the whole thing controllable by
time-machine in tests (implementation.md §5: never sleep).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings

from common.redis import get_redis

# KEYS[1] = bucket key
# ARGV = capacity, refill_per_second, now, requested, ttl
_CONSUME_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

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

local allowed = 0
if tokens >= requested then
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

    def consume(self, tokens: float = 1, *, now: float | None = None) -> RateLimitResult:
        now = time.time() if now is None else now
        allowed, remaining_raw = self._script(
            keys=[self.key],
            args=[self.capacity, self.refill_per_second, now, tokens, self._ttl],
        )
        remaining = float(remaining_raw)

        retry_after = 0.0
        if not allowed:
            retry_after = max(0.0, (tokens - remaining) / self.refill_per_second)

        return RateLimitResult(allowed=bool(allowed), remaining=remaining, retry_after=retry_after)

    def reset(self) -> None:
        self._redis.delete(self.key)
