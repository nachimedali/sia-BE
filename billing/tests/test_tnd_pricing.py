"""TN-02: the TND price is a conversion (BUILD-PLAN L-4, amended 2026-11-01).

The one exception to "a price per currency is a decision": the USD price x a
staff-set rate, rounded to the whole dinar, plus VAT. Every number is a row —
the rate and rounding on `Currency`, the VAT on `TaxRate` — and a missing one
refuses the sale rather than quoting zero.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from billing.models import Currency, TaxRate
from billing.services import pricing

pytestmark = pytest.mark.django_db


@pytest.fixture
def usd() -> Currency:
    return Currency.objects.create(code="USD", name="US dollar", symbol="$", minor_units=2)


@pytest.fixture
def tnd() -> Currency:
    return Currency.objects.create(
        code="TND",
        name="Tunisian dinar",
        symbol="DT",
        minor_units=3,
        symbol_first=False,
        gateway="konnect",
        rate_from_usd=Decimal("2.987600"),
        round_to_minor=1000,
    )


@pytest.fixture
def tn_vat() -> TaxRate:
    return TaxRate.objects.create(
        country="TN", rate=Decimal("0.1900"), label="TVA", valid_from=dt.date(2018, 1, 1)
    )


def test_49_dollars_converts_rounds_to_the_dinar_and_adds_19_percent(usd, tnd, tn_vat) -> None:
    quote = pricing.converted_quote(4900, source=usd, target=tnd, country="TN")

    # 49 x 2.9876 = 146.3924 → 146 DT; VAT 19% = 27.740 DT.
    assert quote.net_minor == 146_000
    assert quote.vat_rate == Decimal("0.1900")
    assert quote.vat_minor == 27_740
    assert quote.total_minor == 173_740
    assert quote.rate_from_usd == Decimal("2.987600")
    assert tnd.format(quote.total_minor) == "173.740 DT"


def test_half_a_dinar_rounds_up(usd, tnd, tn_vat) -> None:
    tnd.rate_from_usd = Decimal("3.010000")
    tnd.save()
    # 50 x 3.01 = 150.5 → 151, never banker's 150.
    assert pricing.converted_quote(5000, source=usd, target=tnd, country="TN").net_minor == 151_000


def test_vat_rounds_half_up_to_the_millime(usd, tnd, tn_vat) -> None:
    tnd.round_to_minor = 1
    tnd.rate_from_usd = Decimal("1")
    tnd.save()
    # $0.15 → 0.150 DT net; 150 x 0.19 = 28.5 millimes → 29.
    assert pricing.converted_quote(15, source=usd, target=tnd, country="TN").vat_minor == 29


def test_no_rate_means_not_on_sale(usd, tnd, tn_vat) -> None:
    tnd.rate_from_usd = None
    tnd.save()
    with pytest.raises(pricing.CurrencyNotOnSaleError):
        pricing.converted_quote(4900, source=usd, target=tnd, country="TN")


def test_no_tax_rate_refuses_rather_than_charging_zero_vat(usd, tnd) -> None:
    with pytest.raises(pricing.CurrencyNotOnSaleError):
        pricing.converted_quote(4900, source=usd, target=tnd, country="TN")


def test_the_newest_rate_in_force_applies_and_a_future_one_does_not(usd, tnd, tn_vat) -> None:
    today = dt.date(2026, 11, 1)
    TaxRate.objects.create(country="TN", rate=Decimal("0.2100"), valid_from=dt.date(2027, 1, 1))
    assert pricing.tax_rate_for("TN", on=today) == Decimal("0.1900")
    assert pricing.tax_rate_for("TN", on=dt.date(2027, 1, 1)) == Decimal("0.2100")


def test_only_dollars_convert(tnd, tn_vat) -> None:
    eur = Currency.objects.create(code="EUR", name="Euro", minor_units=2)
    with pytest.raises(ValueError):
        pricing.converted_quote(4900, source=eur, target=tnd, country="TN")
