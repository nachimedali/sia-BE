"""Fixtures shared by the billing suite.

`workspace` used to live here too, copied from file to file until this module
collapsed them into one; it has since moved to the root conftest, because
content/tests needed the exact same fixture (same reasoning, one app wider).
"""

from __future__ import annotations

# `_reset_gateway` moved to the root conftest once `workspaces/tests` began
# driving the gateway too — the same one-copy-per-file collapse this module's
# own docstring describes, one app wider again.
# TN payments (`payments-tunisia.md`) ------------------------------------------
import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_prepaid_gateway() -> Iterator[None]:
    """Module-level like the billing fake, so a test can read what a view sent."""
    from billing.gateways.prepaid import _fake

    _fake.clear()
    yield
    _fake.clear()


@pytest.fixture
def konnect() -> Any:
    from billing.gateways.prepaid import _fake

    return _fake


@pytest.fixture
def tnd(db: None) -> Any:
    """TND on sale at 3.0 DT per dollar, 19% VAT and a 1.000 DT stamp, with the
    TN flag on for everyone."""
    from billing.models import Currency, FeatureFlag, TaxRate, TaxStamp
    from billing.services.flags import TN_PAYMENTS_V1

    Currency.objects.get_or_create(code="USD", defaults={"name": "US dollar", "symbol": "$"})
    currency = Currency.objects.create(
        code="TND",
        name="Tunisian dinar",
        symbol="DT",
        minor_units=3,
        symbol_first=False,
        gateway="konnect",
        rate_from_usd=Decimal("3"),
        round_to_minor=1000,
        market_country="TN",
    )
    TaxRate.objects.create(
        country="TN", rate=Decimal("0.19"), label="TVA", valid_from=dt.date(2018, 1, 1)
    )
    TaxStamp.objects.create(country="TN", amount_minor=1000, valid_from=dt.date(2018, 1, 1))
    FeatureFlag.objects.create(organization=None, key=TN_PAYMENTS_V1, enabled=True)
    return currency


@pytest.fixture
def tn_workspace(workspace: Any, tnd: Any) -> Any:
    """An organization locked to TND that has already used its trial."""
    from django.utils import timezone

    organization = workspace.organization
    organization.billing_currency = tnd
    organization.trial_ends_at = timezone.now() - dt.timedelta(days=30)
    organization.save()
    return workspace
