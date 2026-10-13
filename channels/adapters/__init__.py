"""The publishing port's resolver.

`SOCIAL_PROVIDER` chooses the vendor (`social_providers.registry`); the fake
answers in tests, in e2e, and — in auto mode — until the chosen provider is
ready. Callers import `get_platform_adapter` from here, never from a vendor
module, so a switch touches no call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from channels.adapters.base import PlatformAdapter


def get_platform_adapter() -> PlatformAdapter:
    from social_providers import registry

    if registry.use_fakes("publish"):
        from channels.adapters.fake import _fake_adapter

        return _fake_adapter
    adapter: PlatformAdapter = registry.real("publish")
    return adapter


def get_connect_adapter() -> PlatformAdapter:
    """The adapter that connects and disconnects accounts. Its own port, so a
    provider can connect before it can publish (SocialAPI: SA-21 before SA-32);
    the fake answers in the same cases as for publishing."""
    from social_providers import registry

    if registry.use_fakes("connect"):
        from channels.adapters.fake import _fake_adapter

        return _fake_adapter
    adapter: PlatformAdapter = registry.real("connect")
    return adapter


def provider_key(port: str) -> str:
    """Which vendor serves `port` right now — stamped on accounts (`connect`)
    and targets (`publish`) so their provider ids only ever go back to it."""
    from social_providers import registry

    return "fake" if registry.use_fakes(port) else registry.selected_key()
