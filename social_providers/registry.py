"""Which vendor publishes, measures and reads comments (`social-provider-plan.md`).

One setting chooses: `SOCIAL_PROVIDER` — `socialapi` (primary since
2026-10-12) or `zernio` (the kept alternative). Each provider is a row in
`PROVIDERS` naming its credential and its three port implementations, so
choosing one is data, and a port a provider does not have yet is a visible
`None`, not a missing import discovered at publish time.

**Fakes are explicit or automatic, never silent in production.**
`USE_FAKE_PLATFORM_ADAPTER` is `True` (fakes), `False` (the real provider) or
blank — *auto*: the fakes until the selected provider is **ready**, meaning it
has its key and the port being asked for exists. `prod.py` pins `False`, so
production either runs the real provider or refuses loudly
(`ProviderNotReadyError`, and the `social_providers.E002` system check at
deploy). Development keeps working while a port is still being built, and says
so in the log rather than pretending.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from django.conf import settings

from common.exceptions import ProviderError

logger = logging.getLogger(__name__)

#: `connect` (brand, sign-in, choose a Page, disconnect) is its own port so a
#: provider can connect accounts before it can publish through them (SA-21
#: before SA-32) — in production both are required (`checks`).
PORTS = ("connect", "publish", "metrics", "audience")


class ProviderNotReadyError(ProviderError):
    """The selected provider cannot serve this port: no key, or the adapter
    is not built yet. Raised instead of falling back, outside auto mode —
    a premature switch must fail where somebody will see it."""

    default_code = "social_provider_not_ready"
    default_detail = "The social provider is not ready for this operation."


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    credential_setting: str
    #: `"module:attribute"` per port; `None` while the port is not built.
    connect: str | None
    publish: str | None
    metrics: str | None
    audience: str | None

    def port(self, name: str) -> str | None:
        value: str | None = getattr(self, name)
        return value

    @property
    def has_credential(self) -> bool:
        return bool(getattr(settings, self.credential_setting, ""))

    def ready(self, port: str) -> bool:
        return self.has_credential and self.port(port) is not None


PROVIDERS: dict[str, ProviderSpec] = {
    "socialapi": ProviderSpec(
        key="socialapi",
        label="SocialAPI.ai",
        credential_setting="SOCIALAPI_API_KEY",
        # Built step by step (SA-21/32, SA-50, SA-60); each lands by filling
        # its path here, and nothing else changes for callers.
        connect="social_providers.socialapi.publish:SocialAPIAdapter",
        publish="social_providers.socialapi.publish:SocialAPIAdapter",
        metrics=None,
        audience=None,
    ),
    "zernio": ProviderSpec(
        key="zernio",
        label="Zernio",
        credential_setting="ZERNIO_API_KEY",
        connect="channels.adapters.zernio:ZernioAdapter",
        publish="channels.adapters.zernio:ZernioAdapter",
        metrics="analytics.providers.zernio:ZernioMetricsProvider",
        # Zernio's comment reading lives on its metrics provider (one class
        # implements both protocols), so the audience port names the same one.
        audience="analytics.providers.zernio:ZernioMetricsProvider",
    ),
}


def selected_key() -> str:
    """The `SOCIAL_PROVIDER` setting, normalised; SocialAPI when unset."""
    return str(getattr(settings, "SOCIAL_PROVIDER", "") or "socialapi").lower()


def selected() -> ProviderSpec:
    key = selected_key()
    spec = PROVIDERS.get(key)
    if spec is None:
        raise ProviderNotReadyError(
            f"Unknown SOCIAL_PROVIDER {key!r}.",
            detail={"social_provider": key, "known": sorted(PROVIDERS)},
        )
    return spec


def use_fakes(port: str) -> bool:
    """Whether `port` answers from the fakes right now."""
    explicit = getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", None)
    if explicit is not None:
        return bool(explicit)
    spec = selected()
    if spec.ready(port):
        return False
    logger.info(
        "social provider not ready, using the fake",
        extra={"social_provider": spec.key, "port": port, "has_key": spec.has_credential},
    )
    return True


def real(port: str) -> Any:
    """An instance of the selected provider's `port`, or `ProviderNotReadyError`."""
    if port not in PORTS:
        raise ValueError(f"unknown port {port!r}")
    spec = selected()
    path = spec.port(port)
    if path is None:
        raise ProviderNotReadyError(
            f"{spec.label} does not serve {port} yet. Set USE_FAKE_PLATFORM_ADAPTER=True to "
            f"use the fake, or SOCIAL_PROVIDER=zernio for the alternative provider.",
            detail={"social_provider": spec.key, "port": port},
        )
    if not spec.has_credential:
        raise ProviderNotReadyError(
            f"{spec.label} has no API key ({spec.credential_setting}).",
            detail={"social_provider": spec.key, "port": port},
        )
    return port_class(port)()


def port_class(port: str) -> Any:
    """The selected provider's class for `port`, not instantiated — for reading
    what it declares (which platforms pause for a choice, which it cannot
    connect) without needing its key. `None` while the port is not built."""
    path = selected().port(port)
    if path is None:
        return None
    module, _, attribute = path.partition(":")
    return getattr(import_module(module), attribute)


def status() -> dict[str, Any]:
    """What the cockpit and `socialapi_check` print: the choice and each port's state."""
    spec = selected()
    explicit = getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", None)
    return {
        "provider": spec.key,
        "label": spec.label,
        "has_credential": spec.has_credential,
        "fake_mode": "auto" if explicit is None else ("on" if explicit else "off"),
        "ports": {
            port: {
                "built": spec.port(port) is not None,
                "serving": "fake" if use_fakes(port) else spec.key,
            }
            for port in PORTS
        },
    }
