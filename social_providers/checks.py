"""Startup checks for the social provider (Django's system-check framework).

They run on `manage.py check`, on every `runserver`, and — the ones tagged
`deploy` — on `manage.py check --deploy`, which is where a release must stop
if production would publish to nothing.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from social_providers.registry import PORTS, PROVIDERS, selected_key


@register(Tags.compatibility)
def check_provider_known(app_configs: Any, **kwargs: Any) -> list[Error]:
    key = selected_key()
    if key in PROVIDERS:
        return []
    return [
        Error(
            f"SOCIAL_PROVIDER={key!r} is not a known provider.",
            hint=f"Use one of: {', '.join(sorted(PROVIDERS))}.",
            id="social_providers.E001",
        )
    ]


def _unready(key: str) -> list[str]:
    spec = PROVIDERS[key]
    return [port for port in PORTS if not spec.ready(port)]


@register(Tags.compatibility)
def check_provider_ready(app_configs: Any, **kwargs: Any) -> list[Warning]:
    """In development an unready port is served by the fake (auto mode) — worth
    a warning, not a stop."""
    key = selected_key()
    if key not in PROVIDERS or getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", None) is True:
        return []
    missing = _unready(key)
    if not missing:
        return []
    return [
        Warning(
            f"{PROVIDERS[key].label} is not ready for: {', '.join(missing)}. "
            "Those ports answer from the fakes until it is.",
            hint="Add the provider's API key, or wait for the port to be built "
            "(social-provider-plan.md).",
            id="social_providers.W001",
        )
    ]


@register(Tags.compatibility, deploy=True)
def check_provider_ready_for_deploy(app_configs: Any, **kwargs: Any) -> list[Error]:
    """A deploy that would publish, measure or read comments through nothing."""
    key = selected_key()
    if key not in PROVIDERS:
        return []
    if getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", None) is not False:
        return [
            Error(
                "USE_FAKE_PLATFORM_ADAPTER must be False in a deployment.",
                id="social_providers.E003",
            )
        ]
    missing = _unready(key)
    if not missing:
        return []
    return [
        Error(
            f"{PROVIDERS[key].label} cannot serve: {', '.join(missing)}.",
            hint="Set its API key, or SOCIAL_PROVIDER=zernio for the alternative provider.",
            id="social_providers.E002",
        )
    ]


@register(Tags.compatibility, deploy=True)
def check_webhook_secret(app_configs: Any, **kwargs: Any) -> list[Warning]:
    """SocialAPI deployed without its webhook secret still works — the
    reconciler and the comment poll cover every event — but outcomes arrive
    minutes later and the top plan's real-time comments do not arrive at all."""
    secret = str(getattr(settings, "SOCIALAPI_WEBHOOK_SECRET", "") or "")
    if selected_key() != "socialapi" or secret:
        return []
    return [
        Warning(
            "SOCIALAPI_WEBHOOK_SECRET is not set: webhooks are refused and every event "
            "arrives by polling.",
            hint="manage.py socialapi_webhooks register --url https://<host>/api/v1/webhooks/socialapi/",
            id="social_providers.W002",
        )
    ]
