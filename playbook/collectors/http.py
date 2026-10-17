"""Shared HTTP plumbing for the real collectors (ported from trendgen).

Two providers, one set of rules about the outside world: one timeout, one way a
non-2xx becomes an exception, one reading of a provider's timestamp and counts.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from playbook.collectors.base import CollectorError

logger = logging.getLogger(__name__)

#: What each real adapter needs before it can answer. Google Trends is absent:
#: its feed is public, so it is the provider whose real adapter runs on a
#: fresh checkout.
CREDENTIALS: dict[str, tuple[str, ...]] = {"youtube": ("YOUTUBE_API_KEY",)}


def missing_credentials(provider: str) -> list[str]:
    return [name for name in CREDENTIALS.get(provider, ()) if not getattr(settings, name, "")]


def is_configured(provider: str) -> bool:
    return not missing_credentials(provider)


def require_credentials(provider: str) -> None:
    """Fail before the wire when a key is absent — otherwise a blank key comes
    back as a 400 and reads as a provider failure rather than a setup gap."""
    missing = missing_credentials(provider)
    if missing:
        raise CollectorError(
            f"{provider} is not configured", detail={"provider": provider, "missing": missing}
        )


def client(*, base_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=settings.PLAYBOOK_COLLECTOR_TIMEOUT_SECONDS,
        headers={"User-Agent": settings.PLAYBOOK_USER_AGENT},
    )


def fetch_response(provider: str, http: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    try:
        response = http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise CollectorError(f"{provider} unreachable", detail={"provider": provider}) from exc
    if response.status_code >= 400:
        raise CollectorError(
            f"{provider} returned {response.status_code}",
            detail={"provider": provider, "status": response.status_code},
        )
    return response


def get_json(provider: str, http: httpx.Client, url: str, **kwargs: Any) -> dict[str, Any]:
    response = fetch_response(provider, http, url, **kwargs)
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise CollectorError(
            f"{provider} returned non-JSON", detail={"provider": provider}
        ) from exc
    return payload


def parse_timestamp(value: Any) -> dt.datetime:
    """A provider's timestamp, or now.

    An unreadable date costs a wrong recency score, which the next read
    corrects; dropping the item costs the item, which nothing corrects. RSS
    dates (RFC 822) and API dates (ISO 8601) both arrive here.
    """
    if isinstance(value, dt.datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value, dt.UTC)
    if value:
        text = str(value).strip()
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                logger.warning("unparseable provider timestamp", extra={"value": text[:64]})
                return timezone.now()
        return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt.UTC)
    return timezone.now()


def as_int(value: Any) -> int | None:
    """A reported count, or None. Never 0 for "absent" (rule 4)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise_hashtags(values: Iterable[str]) -> list[str]:
    """One spelling of a tag: `#Tunisia` and `#tunisia` are one tag."""
    tags = {
        f"#{text.strip().lstrip('#').replace(' ', '').lower()}"
        for text in values
        if text and text.strip().strip("#")
    }
    return sorted(tags)


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def iso_duration_seconds(value: Any) -> int | None:
    """`PT1M3S` → 63. YouTube's `contentDetails.duration`; None when absent or
    unreadable (a live stream reports `P0D`, which is no duration at all)."""
    match = _DURATION.match(str(value or ""))
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    return total or None
