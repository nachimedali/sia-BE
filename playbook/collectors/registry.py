"""Which collector answers for a provider.

A real adapter is used when its credentials are present and fakes are not
forced. Absent credentials select the fake **only where samples may be shown**
(`PLAYBOOK_SHOW_SAMPLE`: dev, test, e2e), so a fresh checkout fills its wall.
Anywhere else the real adapter answers and refuses before the wire
(`require_credentials`), which records "not configured" on the source rather
than writing fixture rows into a production database.
"""

from __future__ import annotations

from django.conf import settings

from playbook.collectors.base import Collector
from playbook.collectors.fake import FakeCollector
from playbook.collectors.google_trends import GoogleTrendsCollector
from playbook.collectors.http import is_configured
from playbook.collectors.youtube import YouTubeCollector

REAL: dict[str, type[Collector]] = {
    "youtube": YouTubeCollector,
    "google_trends": GoogleTrendsCollector,
}

#: Test-installed overrides, cleared by an autouse fixture.
_overrides: dict[str, Collector] = {}


def set_override(provider: str, collector: Collector | None) -> None:
    if collector is None:
        _overrides.pop(provider, None)
    else:
        _overrides[provider] = collector


def clear_overrides() -> None:
    _overrides.clear()


def uses_fake(provider: str) -> bool:
    if settings.USE_FAKE_PLAYBOOK_COLLECTORS:
        return True
    return not is_configured(provider) and bool(settings.PLAYBOOK_SHOW_SAMPLE)


def get_collector(provider: str) -> Collector:
    if provider not in REAL:
        raise KeyError(f"unknown playbook provider {provider!r}; known: {sorted(REAL)}")
    if provider in _overrides:
        return _overrides[provider]
    if uses_fake(provider):
        return FakeCollector(provider=provider)
    return REAL[provider]()
