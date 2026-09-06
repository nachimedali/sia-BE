"""The measurement port (BUILD-PLAN L-5, P0-27..P0-31).

Kept structurally apart from `channels.adapters`, which publishes. Publishing
sends data out; measurement pulls it in, and the two vendors need not be the
same one. Neither port carries the other's methods — that separation is what
makes a later swap a code change rather than an excavation, and
`analytics/tests/test_port_separation.py` asserts it rather than trusting it.
"""

from __future__ import annotations

from django.conf import settings

from analytics.providers.base import (
    MetricsError,
    MetricsProvider,
    MetricsProviderRegistry,
    MetricsUnsupportedError,
    RawMetricPayload,
)

__all__ = [
    "MetricsError",
    "MetricsProvider",
    "MetricsProviderRegistry",
    "MetricsUnsupportedError",
    "RawMetricPayload",
    "get_metrics_registry",
]


def get_metrics_registry() -> MetricsProviderRegistry:
    """The configured registry.

    One provider today. It is still a registry rather than a single object
    because `resolve(platform, metric)` returning `None` is the mechanism that
    makes an uncovered platform report unavailable instead of zero (A-19), and
    that mechanism should not have to be invented on the day a second vendor
    arrives.
    """
    if getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", False):
        from analytics.providers.fake import fake_provider

        return MetricsProviderRegistry([fake_provider()])

    from analytics.providers.zernio import ZernioMetricsProvider

    return MetricsProviderRegistry([ZernioMetricsProvider()])
