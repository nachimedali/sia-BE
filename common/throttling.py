"""Per-IP rate limits on the auth endpoints (design.md §11).

Credential stuffing is the threat: an attacker replays a leaked password list
against /auth/login/. Buckets are keyed on client IP and back onto the Redis
token bucket from `common.ratelimit`, so limits hold across every web process
rather than per-process as an in-memory limiter would.

Raising `RateLimited` rather than DRF's `Throttled` keeps the response inside
the single error envelope with a `Retry-After` header (design.md §7.1).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.throttling import BaseThrottle

from common.exceptions import RateLimited
from common.ratelimit import TokenBucket


def client_ip(request: Request) -> str:
    """Trusts X-Forwarded-For's left-most entry.

    Correct only behind a proxy that overwrites the header — which is the
    deployment shape (design.md §5). Direct exposure would let a client forge it.
    """
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", ""))
    if forwarded:
        return forwarded.split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", "unknown"))


class IPTokenBucketThrottle(BaseThrottle):
    """Base class: subclasses set `scope`, `capacity` and `refill_per_second`.

    Both are overridable per scope via the `AUTH_THROTTLES` setting, so an
    operator can loosen or tighten a limit without a deploy — and so the E2E
    suite, which drives many registrations from one address, is not fighting
    the limiter it is not there to test.
    """

    scope: str = "default"
    capacity: int = 30
    refill_per_second: float = 0.5

    def _override(self, key: str, fallback: float) -> float:
        overrides = getattr(settings, "AUTH_THROTTLES", {})
        return float(overrides.get(self.scope, {}).get(key, fallback))

    def allow_request(self, request: Request, view: Any) -> bool:
        bucket = TokenBucket(
            key=f"{self.scope}:{client_ip(request)}",
            capacity=int(self._override("capacity", self.capacity)),
            refill_per_second=self._override("refill_per_second", self.refill_per_second),
        )
        result = bucket.consume()
        if not result.allowed:
            raise RateLimited(
                "Too many attempts. Please wait before trying again.",
                retry_after=result.retry_after,
            )
        return True


class LoginThrottle(IPTokenBucketThrottle):
    # 10 attempts, then one more every 30s. Generous enough for a person
    # fumbling a password, useless for walking a list.
    scope = "auth:login"
    capacity = 10
    refill_per_second = 1 / 30


class RegisterThrottle(IPTokenBucketThrottle):
    scope = "auth:register"
    capacity = 5
    refill_per_second = 1 / 120


class PasswordResetThrottle(IPTokenBucketThrottle):
    # Reset requests send mail to an address the requester may not own, so this
    # is an outbound-spam control as much as an auth control.
    scope = "auth:reset"
    capacity = 5
    refill_per_second = 1 / 120


class VerificationResendThrottle(IPTokenBucketThrottle):
    scope = "auth:resend"
    capacity = 5
    refill_per_second = 1 / 60
