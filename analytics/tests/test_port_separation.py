"""Publishing and measurement are separate ports (P0-28, Part 7 rule 18).

**Structural, not conventional.** A comment saying "do not put metrics on the
publish adapter" survives exactly until someone is in a hurry. These tests fail
the build instead.

The rule cuts both ways and both directions matter. A metrics method on the
publish port means the day measurement moves to another vendor, every publish
call site is in the blast radius. A publish method on the metrics port means a
measurement vendor could be handed a post to send.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from analytics.providers.fake import FakeMetricsProvider
from analytics.providers.zernio import ZernioMetricsProvider
from channels.adapters.base import PlatformAdapter
from channels.adapters.fake import FakePlatformAdapter
from channels.adapters.zernio import ZernioAdapter

#: Anything that acquires measurement. Naming them explicitly rather than
#: pattern-matching on "fetch": `fetch_metrics` and `fetch_comments` are the
#: two the publish port actually carried, and a new one should have to be added
#: here deliberately.
MEASUREMENT_METHODS = (
    "fetch_metrics",
    "fetch_comments",
    "fetch_account_stats",
    "fetch_reactions",
    "changed_since",
    "supports",
    "supports_comments",
)

PUBLISH_METHODS = (
    "publish",
    "connect_url",
    "resolve_callback",
    "select_target",
    "ensure_profile",
    "disconnect",
)


@pytest.mark.parametrize("adapter", [FakePlatformAdapter, ZernioAdapter])
@pytest.mark.parametrize("method", MEASUREMENT_METHODS)
def test_the_publish_port_carries_no_measurement_method(adapter: Any, method: str) -> None:
    assert not hasattr(adapter, method), (
        f"{adapter.__name__}.{method} is measurement. It belongs on "
        f"analytics.providers.MetricsProvider — see P0-28."
    )


@pytest.mark.parametrize("provider", [FakeMetricsProvider, ZernioMetricsProvider])
@pytest.mark.parametrize("method", PUBLISH_METHODS)
def test_the_metrics_port_carries_no_publish_method(provider: Any, method: str) -> None:
    assert not hasattr(provider, method), (
        f"{provider.__name__}.{method} publishes. It belongs on "
        f"channels.adapters.PlatformAdapter — see P0-28."
    )


@pytest.mark.parametrize("method", MEASUREMENT_METHODS)
def test_the_publish_protocol_itself_declares_no_measurement(method: str) -> None:
    """The Protocol, not just the implementations. A declared-but-unimplemented
    method is still a promise the next adapter author will try to keep."""
    assert method not in {name for name, _ in inspect.getmembers(PlatformAdapter)}


def test_the_two_ports_share_no_module() -> None:
    """Import-level separation. If the metrics providers lived under
    `channels.adapters`, every rule above would be one refactor from being
    vacuously true."""
    assert ZernioMetricsProvider.__module__.startswith("analytics.providers")
    assert ZernioAdapter.__module__.startswith("channels.adapters")
