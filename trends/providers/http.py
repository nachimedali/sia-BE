"""Shared HTTP plumbing for the real trend adapters.

Four adapters, one set of rules about talking to the outside world, so a change
to how vendors are called — a timeout, a retry, how a non-2xx becomes an
exception — is one edit rather than four.

**Trend traffic yields.** Every call goes through `ProviderRateLimiter.
consume_for_background`, the low-priority half of the shared bucket Phase 6
built (design.md §5.1: `trends_q` is lowest priority, "silent retry, last
corpus stays valid"). Harvesting must never take capacity a publish needs, and
a refused harvest is not an incident: the cached corpus is still servable.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from common.ratelimit import ProviderRateLimiter
from common.timestamps import parse_or_none
from trends.providers.base import TrendVendorError

logger = logging.getLogger(__name__)

#: Sized well below any vendor's documented ceiling: harvesting is not
#: latency-sensitive, and the corpus it feeds is 30 days old by design (D11).
VENDOR_CAPACITY = 60
VENDOR_REFILL_PER_SECOND = 1.0


class VendorBusyError(TrendVendorError):
    """Our own limiter refused before the vendor had to. Not a failure — the
    caller logs it and keeps whatever corpus it already had."""

    default_code = "trend_vendor_busy"
    default_detail = "Trend harvesting is yielding to higher-priority traffic."


def client(*, base_url: str, headers: dict[str, str] | None = None) -> Any:
    import httpx

    return httpx.Client(
        base_url=base_url,
        headers=headers or {},
        timeout=settings.TREND_VENDOR_TIMEOUT_SECONDS,
    )


def get_json(vendor: str, http: Any, url: str, **kwargs: Any) -> dict[str, Any]:
    """One GET, rate-limited and classified."""
    # Keyed on the vendor, which is the outbound dependency `ProviderRateLimiter`
    # exists to bound — and deliberately the same bucket Phase 11's metrics
    # polling will draw from for the same vendor, since one bucket with a
    # publish-only reserve is the whole point of not sizing two independently.
    allowance = ProviderRateLimiter(
        vendor, capacity=VENDOR_CAPACITY, refill_per_second=VENDOR_REFILL_PER_SECOND
    ).consume_for_background()
    if not allowance:
        raise VendorBusyError(detail={"vendor": vendor, "retry_after": allowance.retry_after})

    response = http.get(url, **kwargs)
    if response.status_code >= 400:
        raise TrendVendorError(
            f"{vendor} returned {response.status_code}.",
            detail={"vendor": vendor, "status": response.status_code, "body": response.text[:500]},
        )
    parsed: dict[str, Any] = response.json()
    return parsed


def newer_than(items: list[Any], since: dt.datetime | None, key: Any) -> list[Any]:
    """Client-side watermark filter, for the vendors whose API has no `since`
    parameter to pass one to."""
    if since is None:
        return items
    return [item for item in items if key(item) > since]


def parse_timestamp(value: Any) -> dt.datetime:
    """`common.timestamps.parse_or_none`, with this pipeline's answer for the
    unreadable case: "now" rather than dropping the item. A wrong recency score
    is recoverable on the next extraction; a lost item is not."""
    parsed = parse_or_none(value)
    if parsed is None:
        if value:
            logger.warning("unparseable vendor timestamp", extra={"value": str(value)[:64]})
        return timezone.now()
    return parsed
