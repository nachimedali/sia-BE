"""Per-country pricing (per-currency catalogue rows).

The rule the whole feature rests on: **a price per currency is a decision, not
a conversion.** €37 is not $37 through today's FX rate — it is usually a
rounder number and often a different one relative to local buying power.
Storing a base price and converting at read time would make every market's
price a function of the dollar and move it every morning.

Three things are asserted here: the resolution order, the fallbacks that keep a
half-configured catalogue selling, and the arithmetic that makes zero-decimal
currencies safe.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from billing.models import Currency, Pack, PackPrice, Plan, PlanPrice
from billing.services import pricing

pytestmark = pytest.mark.django_db


@pytest.fixture
def currencies(db: None) -> dict[str, Currency]:
    call_command("seed_currencies", verbosity=0)
    return {c.code: c for c in Currency.objects.all()}


def _price(plan: Plan, currency: Currency, *, monthly: int, default: bool = False) -> PlanPrice:
    return PlanPrice.objects.create(
        plan=plan, currency=currency, monthly_cents=monthly, is_default=default
    )


# -----------------------------------------------------------------------------
# Resolution order
# -----------------------------------------------------------------------------
def test_an_organization_is_charged_in_its_own_currency(
    plans: dict[str, Any], organization: Any, currencies: dict[str, Currency]
) -> None:
    plan = plans["pro"]
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["USD"], monthly=3700, default=True)
    _price(plan, currencies["MAD"], monthly=39000)

    organization.billing_currency = currencies["MAD"]
    organization.save(update_fields=["billing_currency"])

    resolved = pricing.plan_price(plan, organization=organization)

    assert resolved.amount_minor == 39000
    assert resolved.currency.code == "MAD"
    assert resolved.is_fallback is False


def test_an_unpriced_currency_falls_back_to_the_default_and_says_so(
    plans: dict[str, Any], organization: Any, currencies: dict[str, Currency]
) -> None:
    """An unpriced market is an operator's to-do, not a customer's error page —
    so the sale still happens, and `is_fallback` is what lets the surface say
    "priced in USD because we do not price this in yours yet"."""
    plan = plans["pro"]
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["USD"], monthly=3700, default=True)

    organization.billing_currency = currencies["JPY"]
    organization.save(update_fields=["billing_currency"])

    resolved = pricing.plan_price(plan, organization=organization)

    assert resolved.currency.code == "USD"
    assert resolved.is_fallback is True


def test_an_organization_with_no_currency_gets_the_default(
    plans: dict[str, Any], organization: Any, currencies: dict[str, Currency]
) -> None:
    """Null means "whatever the catalogue's default is", which is why the column
    is nullable rather than defaulted to USD — a default would silently make
    every new market American."""
    plan = plans["pro"]
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["EUR"], monthly=3500, default=True)

    assert organization.billing_currency is None

    resolved = pricing.plan_price(plan, organization=organization)

    assert resolved.currency.code == "EUR"
    assert resolved.is_fallback is False


def test_a_plan_with_no_price_rows_falls_back_to_its_own_columns(
    plans: dict[str, Any], organization: Any
) -> None:
    """The dual-read half of the migration: a deployment part-way through the
    backfill quotes a price rather than zero."""
    plan = plans["pro"]
    PlanPrice.objects.filter(plan=plan).delete()

    resolved = pricing.plan_price(plan, organization=organization)

    assert resolved.amount_minor == plan.price_monthly_cents


def test_the_annual_cycle_resolves_separately(
    plans: dict[str, Any], organization: Any, currencies: dict[str, Currency]
) -> None:
    plan = plans["pro"]
    PlanPrice.objects.filter(plan=plan).delete()
    PlanPrice.objects.create(
        plan=plan,
        currency=currencies["EUR"],
        monthly_cents=3500,
        annual_cents=33000,
        is_default=True,
    )

    assert pricing.plan_price(plan, organization=organization).amount_minor == 3500
    assert pricing.plan_price(plan, organization=organization, cycle="annual").amount_minor == 33000


def test_a_blank_stripe_id_on_the_row_falls_through_to_the_plan(
    plans: dict[str, Any], organization: Any, currencies: dict[str, Currency]
) -> None:
    """Otherwise adding a second currency would break checkout in the first
    one: the new row would shadow the plan's configured Stripe price with a
    blank."""
    plan = plans["pro"]
    plan.stripe_price_id_monthly = "price_legacy"
    plan.save(update_fields=["stripe_price_id_monthly"])
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["USD"], monthly=3700, default=True)

    assert pricing.plan_price(plan, organization=organization).stripe_price_id == "price_legacy"


# -----------------------------------------------------------------------------
# Packs
# -----------------------------------------------------------------------------
def test_a_pack_is_priced_per_currency_but_grants_the_same_units(
    organization: Any, currencies: dict[str, Currency]
) -> None:
    """Varying units as well as price would make two markets incomparable in
    every revenue question anyone later asks."""
    call_command("seed_packs", verbosity=0)
    pack = Pack.objects.first()
    assert pack is not None
    PackPrice.objects.create(pack=pack, currency=currencies["MAD"], amount_cents=10000)

    organization.billing_currency = currencies["MAD"]
    organization.save(update_fields=["billing_currency"])

    resolved = pricing.pack_price(pack, organization=organization)

    assert resolved.amount_minor == 10000
    assert resolved.currency.code == "MAD"
    # Same pack, same grant.
    assert pack.units == Pack.objects.get(pk=pack.pk).units


# -----------------------------------------------------------------------------
# Zero-decimal currencies — the reason `minor_units` exists
# -----------------------------------------------------------------------------
def test_a_zero_decimal_currency_is_not_divided_by_a_hundred(
    currencies: dict[str, Currency],
) -> None:
    """¥3700 is ¥3700, not ¥37.00. Treating it as cents would undercharge by a
    factor of a hundred."""
    assert currencies["JPY"].format(3700) == "¥3700"
    assert currencies["USD"].format(3700) == "$37.00"


def test_the_symbol_can_trail(currencies: dict[str, Currency]) -> None:
    assert currencies["EUR"].format(3500) == "35.00 €"


def test_formatting_handles_a_zero_and_a_negative(currencies: dict[str, Currency]) -> None:
    assert currencies["USD"].format(0) == "$0.00"
    assert currencies["USD"].format(-500) == "$-5.00"


def test_a_code_is_stored_upper_case() -> None:
    currency = Currency.objects.create(code="mad", name="Moroccan dirham")

    assert currency.code == "MAD"


# -----------------------------------------------------------------------------
# Guard rails
# -----------------------------------------------------------------------------
def test_one_default_price_per_plan_is_enforced_by_the_database(
    plans: dict[str, Any], currencies: dict[str, Currency]
) -> None:
    """Two defaults means the resolver picks by row order — a bug that only
    shows up in one market, which is the hardest kind to be told about.

    Surfaces as `ValidationError` rather than `IntegrityError` because
    `CataloguePrice.save` runs `full_clean` first, so an operator in admin gets
    a readable message instead of a database traceback. The constraint is still
    in Postgres, which is what makes it hold for a raw write too.
    """
    plan = plans["advanced"]
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["USD"], monthly=9700, default=True)

    with pytest.raises(ValidationError):
        _price(plan, currencies["EUR"], monthly=9000, default=True)


def test_a_currency_cannot_be_priced_twice_for_one_plan(
    plans: dict[str, Any], currencies: dict[str, Currency]
) -> None:
    plan = plans["advanced"]
    PlanPrice.objects.filter(plan=plan).delete()
    _price(plan, currencies["USD"], monthly=9700)

    with pytest.raises(ValidationError):
        _price(plan, currencies["USD"], monthly=9900)


def test_a_negative_price_is_refused(
    plans: dict[str, Any], currencies: dict[str, Currency]
) -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _price(plans["advanced"], currencies["USD"], monthly=-1)


def test_a_currency_in_use_cannot_be_deleted(
    plans: dict[str, Any], currencies: dict[str, Currency]
) -> None:
    """`PROTECT`, so withdrawing a currency is unsetting *is active* rather
    than deleting rows that prices point at."""
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        currencies["USD"].delete()


# -----------------------------------------------------------------------------
# Seeds and admin reachability
# -----------------------------------------------------------------------------
def test_the_currency_seed_is_idempotent(currencies: dict[str, Currency]) -> None:
    before = Currency.objects.count()
    call_command("seed_currencies", verbosity=0)

    assert Currency.objects.count() == before


def test_seeding_plans_creates_a_default_price_row(plans: dict[str, Any]) -> None:
    """A fresh checkout must have priced plans, or the pricing page renders
    zero."""
    for plan in Plan.objects.all():
        assert plan.prices.filter(is_default=True).exists(), f"{plan.code} has no default price"


def test_currency_and_prices_are_editable_in_admin() -> None:
    from django.contrib import admin

    from billing.admin import PackPriceInline, PlanPriceInline

    assert Currency in admin.site._registry
    assert PlanPriceInline in admin.site._registry[Plan].inlines
    assert PackPriceInline in admin.site._registry[Pack].inlines
