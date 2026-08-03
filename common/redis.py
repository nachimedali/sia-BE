"""Redis client factory.

Kept separate from the Django cache so the rate limiter can run raw commands and
Lua scripts without depending on the cache backend's key mangling.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=4)
def get_redis(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(
        url or settings.RATELIMIT_REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
