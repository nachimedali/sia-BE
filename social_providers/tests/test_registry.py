"""Choosing the social provider (SA-00).

`SOCIAL_PROVIDER` picks the vendor; `USE_FAKE_PLATFORM_ADAPTER` is True, False
or blank (auto). Auto serves the fakes until the chosen provider has its key
**and** the port asked for — and a deployment refuses to start on anything
else, so production can never publish to a fake or to nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.checks import run_checks
from django.test import override_settings

from analytics.providers import get_metrics_registry
from analytics.providers.zernio import ZernioMetricsProvider
from channels.adapters import get_platform_adapter
from channels.adapters.fake import FakePlatformAdapter
from channels.adapters.zernio import ZernioAdapter
from social_providers import registry
from social_providers.registry import ProviderNotReadyError


def check_ids(include_deployment_checks: bool = False) -> set[str]:
    return {
        message.id
        for message in run_checks(include_deployment_checks=include_deployment_checks)
        if message.id and message.id.startswith("social_providers.")
    }


def test_socialapi_is_the_default_and_zernio_the_alternative(settings: Any) -> None:
    settings.SOCIAL_PROVIDER = ""
    assert registry.selected().key == "socialapi"
    settings.SOCIAL_PROVIDER = "zernio"
    assert registry.selected().key == "zernio"


def test_an_unknown_provider_is_refused_and_flagged(settings: Any) -> None:
    settings.SOCIAL_PROVIDER = "buffer"
    with pytest.raises(ProviderNotReadyError, match="Unknown SOCIAL_PROVIDER"):
        registry.selected()
    assert "social_providers.E001" in check_ids()


@override_settings(
    USE_FAKE_PLATFORM_ADAPTER=None, SOCIAL_PROVIDER="socialapi", SOCIALAPI_API_KEY="k"
)
def test_auto_mode_serves_each_port_from_socialapi_once_it_is_built() -> None:
    # Key present: the built ports (connect, publish) are real; measurement is
    # not built yet, so the fake answers it, and says so.
    from social_providers.socialapi.publish import SocialAPIAdapter

    assert registry.use_fakes("publish") is False
    assert isinstance(get_platform_adapter(), SocialAPIAdapter)
    assert registry.use_fakes("metrics") is True
    assert get_metrics_registry().all()[0].key != "socialapi"


@override_settings(USE_FAKE_PLATFORM_ADAPTER=None, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="k")
def test_auto_mode_uses_a_ready_provider() -> None:
    assert registry.use_fakes("publish") is False
    assert isinstance(get_platform_adapter(), ZernioAdapter)
    assert isinstance(get_metrics_registry().all()[0], ZernioMetricsProvider)


@override_settings(USE_FAKE_PLATFORM_ADAPTER=None, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="")
def test_auto_mode_without_a_key_uses_the_fakes() -> None:
    assert isinstance(get_platform_adapter(), FakePlatformAdapter)


@override_settings(
    USE_FAKE_PLATFORM_ADAPTER=False, SOCIAL_PROVIDER="socialapi", SOCIALAPI_API_KEY="k"
)
def test_forcing_the_real_provider_before_a_port_is_built_fails_loudly() -> None:
    from social_providers.socialapi.publish import SocialAPIAdapter

    assert isinstance(get_platform_adapter(), SocialAPIAdapter)
    with pytest.raises(ProviderNotReadyError, match="SOCIAL_PROVIDER=zernio"):
        get_metrics_registry()


@override_settings(USE_FAKE_PLATFORM_ADAPTER=False, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="")
def test_forcing_a_provider_without_its_key_fails_loudly() -> None:
    with pytest.raises(ProviderNotReadyError, match="no API key"):
        get_platform_adapter()


@override_settings(USE_FAKE_PLATFORM_ADAPTER=True, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="k")
def test_forcing_the_fakes_wins() -> None:
    assert isinstance(get_platform_adapter(), FakePlatformAdapter)


@override_settings(
    USE_FAKE_PLATFORM_ADAPTER=None, SOCIAL_PROVIDER="socialapi", SOCIALAPI_API_KEY="k"
)
def test_development_warns_but_runs() -> None:
    ids = check_ids()
    assert "social_providers.W001" in ids
    assert "social_providers.E002" not in ids


@override_settings(USE_FAKE_PLATFORM_ADAPTER=None, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="k")
def test_a_deployment_refuses_fake_mode_auto() -> None:
    assert "social_providers.E003" in check_ids(include_deployment_checks=True)


@override_settings(
    USE_FAKE_PLATFORM_ADAPTER=False, SOCIAL_PROVIDER="socialapi", SOCIALAPI_API_KEY="k"
)
def test_a_deployment_refuses_a_provider_that_cannot_serve() -> None:
    assert "social_providers.E002" in check_ids(include_deployment_checks=True)


@override_settings(USE_FAKE_PLATFORM_ADAPTER=False, SOCIAL_PROVIDER="zernio", ZERNIO_API_KEY="k")
def test_a_ready_deployment_passes() -> None:
    assert check_ids(include_deployment_checks=True) == set()


def test_production_reads_the_selected_providers_key_without_a_fallback() -> None:
    from pathlib import Path

    prod = (Path(__file__).resolve().parents[2] / "config" / "settings" / "prod.py").read_text()
    assert 'SOCIALAPI_API_KEY = env("SOCIALAPI_API_KEY")' in prod
    assert 'ZERNIO_API_KEY = env("ZERNIO_API_KEY")' in prod
    assert "USE_FAKE_PLATFORM_ADAPTER = False" in prod


def test_status_reports_each_port(settings: Any) -> None:
    settings.SOCIAL_PROVIDER = "socialapi"
    settings.SOCIALAPI_API_KEY = "k"
    settings.USE_FAKE_PLATFORM_ADAPTER = None
    state = registry.status()
    assert state["provider"] == "socialapi"
    assert state["fake_mode"] == "auto"
    assert state["ports"]["publish"] == {"built": True, "serving": "socialapi"}
    assert state["ports"]["metrics"] == {"built": False, "serving": "fake"}
