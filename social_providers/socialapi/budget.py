"""SocialAPI's request budget: one pool for the whole team, shared with publishing.

The vendor limits requests per **team**, per minute, across every key and the
dashboard (60/min with ≤2 connected accounts, 600 with 3 to 2,000, 1,200 above —
docs: *Plans & Limits*). So this is one bucket, not one per account like
Zernio's (`common.ratelimit.ProviderRateLimiter` keyed `socialapi:*:-`).

**Publishing is never blocked by capture** (Part 7 rule 11): background calls
stop at a reserve (`SOCIALAPI_PUBLISH_RESERVE_RATIO`) that only publishing may
spend. Two more rules come from the vendor being the authority, not our model
of it:

1. **Calibrated from the answers.** Every response carries
   `X-RateLimit-Limit/Remaining/Reset`. The limit we last saw becomes the
   bucket's capacity (so connecting a third account lifts us from 60 to 600
   without a deploy), and our configured figure is only the starting guess.
2. **The vendor's "no" wins.** When it reports `Remaining: 0`, or answers 429,
   every caller waits until its `Reset` — our bucket may think tokens are left
   (the dashboard, another key, a retry) but the vendor's count is the real
   one. When it reports fewer left than the reserve, background waits too.

Redis down: publishing falls back to the Postgres counter, background is
refused — never unlimited (P0-43, inherited from `ProviderRateLimiter`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.conf import settings
from redis.exceptions import RedisError

from common.ratelimit import ProviderRateLimiter, RateLimitResult
from common.redis import get_redis

logger = logging.getLogger(__name__)

PROVIDER = "socialapi"
LIMIT_KEY = "socialapi:budget:limit"
#: Until when every call must wait (vendor said 0 left, or 429).
HOLD_ALL_KEY = "socialapi:budget:hold_all"
#: Until when background calls must wait (vendor said we are inside the reserve).
HOLD_BACKGROUND_KEY = "socialapi:budget:hold_background"
#: A limit we observed stays trusted this long; a plan change shows up within it.
LIMIT_TTL_SECONDS = 3600


class Priority:
    PUBLISH = "publish"
    BACKGROUND = "background"


@dataclass(frozen=True)
class RateState:
    """What one response said about the budget. `None` = header absent."""

    limit: int | None = None
    remaining: int | None = None
    reset: float | None = None


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def parse_headers(headers: dict[str, str] | object) -> RateState:
    get = getattr(headers, "get", None)
    if get is None:
        return RateState()
    reset_raw = get("X-RateLimit-Reset") or get("Retry-After")
    reset = _int(reset_raw)
    return RateState(
        limit=_int(get("X-RateLimit-Limit")),
        remaining=_int(get("X-RateLimit-Remaining")),
        reset=float(reset) if reset is not None else None,
    )


class TeamBudget:
    def __init__(self) -> None:
        self._redis = get_redis(settings.RATELIMIT_REDIS_URL)

    # --- capacity -------------------------------------------------------------------
    def limit(self) -> int:
        configured = max(1, int(settings.SOCIALAPI_REQUESTS_PER_MINUTE))
        try:
            seen = self._redis.get(LIMIT_KEY)
        except RedisError:
            return configured
        return int(seen) if seen else configured

    def _limiter(self) -> ProviderRateLimiter:
        per_minute = self.limit()
        return ProviderRateLimiter(
            "*",
            None,
            capacity=per_minute,
            refill_per_second=per_minute / 60.0,
            provider=PROVIDER,
            reserve_ratio=float(settings.SOCIALAPI_PUBLISH_RESERVE_RATIO),
        )

    # --- before a call -------------------------------------------------------------
    def acquire(self, priority: str, *, now: float | None = None) -> RateLimitResult:
        """A token for one request, or a refusal saying how long to wait."""
        now = time.time() if now is None else now
        held = self._held_until(HOLD_ALL_KEY)
        if priority != Priority.PUBLISH:
            held = max(held, self._held_until(HOLD_BACKGROUND_KEY))
        if held > now:
            return RateLimitResult(allowed=False, remaining=0.0, retry_after=held - now)
        limiter = self._limiter()
        if priority == Priority.PUBLISH:
            return limiter.consume_for_publish(now=now)
        return limiter.consume_for_background(now=now)

    def _held_until(self, key: str) -> float:
        try:
            value = self._redis.get(key)
        except RedisError:
            return 0.0
        return float(value) if value else 0.0

    # --- after a call ----------------------------------------------------------------
    def observe(
        self, state: RateState, *, status: int | None = None, now: float | None = None
    ) -> None:
        """Learn from the vendor's headers. Never raises: losing a calibration
        costs accuracy, not the call."""
        now = time.time() if now is None else now
        try:
            if state.limit:
                self._redis.set(LIMIT_KEY, state.limit, ex=LIMIT_TTL_SECONDS)
            wait = state.reset if state.reset is not None else 60.0
            if status == 429 or state.remaining == 0:
                self._hold(HOLD_ALL_KEY, now + max(1.0, wait))
                return
            limit = state.limit or self.limit()
            reserve = limit * float(settings.SOCIALAPI_PUBLISH_RESERVE_RATIO)
            if state.remaining is not None and state.remaining < reserve:
                self._hold(HOLD_BACKGROUND_KEY, now + max(1.0, wait))
        except RedisError:
            logger.warning("socialapi budget calibration skipped: redis unavailable")

    def _hold(self, key: str, until: float) -> None:
        current = self._held_until(key)
        if until > current:
            self._redis.set(key, until, ex=max(1, int(until - time.time()) + 5))

    def reset(self) -> None:
        """Tests and the operator's "clear budget" action."""
        self._limiter().reset()
        self._redis.delete(LIMIT_KEY, HOLD_ALL_KEY, HOLD_BACKGROUND_KEY)
