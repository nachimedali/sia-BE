"""Which price a given customer sees (per-country pricing).

One rule, applied in one place: **the organization's currency if this item is
priced in it, otherwise the item's default row.** Never a conversion — an FX
rate applied at read time would make every market's price a function of the
dollar and move it every morning, which is not something anyone would choose
to sell.

The fallback is deliberate and quiet. A plan priced only in USD, bought by a
company set to MAD, charges USD rather than refusing the sale: an unpriced
market is an operator's to-do, not a customer's error page.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.utils import timezone

from billing.models import Currency, Pack, Plan, TaxRate

logger = logging.getLogger(__name__)

#: The currency the catalogue falls back to when an item has no default row at
#: all — which only happens before `seed_currencies` has run. Not a commercial
#: number: it is the code of a row, and which row is *default* is editable.
FALLBACK_CURRENCY_CODE = "USD"


@dataclass(frozen=True)
class ResolvedPrice:
    """An amount, the currency it is in, and how to charge it.

    `is_fallback` is carried rather than inferred so a caller can say "priced
    in USD because we do not price this in MAD yet" instead of quietly
    presenting a foreign currency as though it were chosen.
    """

    amount_minor: int
    currency: Currency
    stripe_price_id: str
    is_fallback: bool = False
    #: Set on a converted (TND) price: `amount_minor` is net, and this VAT is
    #: added at checkout and on the invoice. `None` for a price shown as is.
    tax_rate: Decimal | None = None

    @property
    def display(self) -> str:
        return self.currency.format(self.amount_minor)


def currency_for(organization: Any) -> Currency | None:
    """The organization's billing currency, or `None` if it has not set one."""
    return getattr(organization, "billing_currency", None) if organization is not None else None


def _pick(rows: list[Any], wanted: Currency | None) -> tuple[Any | None, bool]:
    """The row in `wanted`, else the default row, else any row.

    "Else any row" is the last resort and it is ordered, not arbitrary: the
    queryset sorts by currency code, so an item with prices but no default
    still resolves the same way on every request rather than by insertion
    order.
    """
    if wanted is not None:
        for row in rows:
            if row.currency_id == wanted.pk:
                return row, False
    for row in rows:
        if row.is_default:
            return row, wanted is not None
    return (rows[0], wanted is not None) if rows else (None, False)


def plan_price(
    plan: Plan,
    *,
    organization: Any = None,
    cycle: str = "monthly",
    currency: Currency | None = None,
) -> ResolvedPrice:
    """What `plan` costs this organization, per billing cycle.

    `currency` overrides the organization's (a visitor's market before one is
    locked). A converted currency (TND) is the USD price converted, net of VAT.

    Falls back to the legacy columns on `Plan` itself when the plan has no
    price rows — the dual-read half of the migration, so a deployment part-way
    through the backfill still quotes a price rather than zero.
    """
    wanted = currency or currency_for(organization)
    if wanted is not None and _converts(wanted):
        return _converted(plan_price(plan, cycle=cycle), wanted)
    rows = list(plan.prices.select_related("currency"))
    row, is_fallback = _pick(rows, wanted)

    if row is None:
        return _legacy_plan_price(plan, cycle=cycle)

    annual = cycle == "annual"
    return ResolvedPrice(
        amount_minor=row.annual_cents if annual else row.monthly_cents,
        currency=row.currency,
        # A blank id on the row means "no Stripe price configured for this
        # currency yet", not "this plan has none" — so it falls through to the
        # legacy column rather than shadowing it. Without this, adding a second
        # currency would break checkout in the first one.
        stripe_price_id=(
            (row.stripe_price_id_annual or plan.stripe_price_id_annual)
            if annual
            else (row.stripe_price_id_monthly or plan.stripe_price_id_monthly)
        ),
        is_fallback=is_fallback,
    )


def plan_per_workspace_price(
    plan: Plan, *, organization: Any = None, currency: Currency | None = None
) -> ResolvedPrice:
    """What each additional workspace costs (P0-17)."""
    wanted = currency or currency_for(organization)
    if wanted is not None and _converts(wanted):
        return _converted(plan_per_workspace_price(plan), wanted)
    row, is_fallback = _pick(list(plan.prices.select_related("currency")), wanted)

    if row is None:
        return _legacy_plan_price(plan, cycle="per_workspace")
    return ResolvedPrice(
        amount_minor=row.per_workspace_cents,
        currency=row.currency,
        stripe_price_id=row.stripe_price_id_monthly or plan.stripe_price_id_monthly,
        is_fallback=is_fallback,
    )


def pack_price(
    pack: Pack, *, organization: Any = None, currency: Currency | None = None
) -> ResolvedPrice:
    wanted = currency or currency_for(organization)
    if wanted is not None and _converts(wanted):
        return _converted(pack_price(pack), wanted)
    rows = list(pack.prices.select_related("currency"))
    row, is_fallback = _pick(rows, wanted)

    if row is None:
        return ResolvedPrice(
            amount_minor=pack.price_cents,
            currency=_fallback_currency(pack.currency),
            stripe_price_id=pack.stripe_price_id,
        )
    return ResolvedPrice(
        amount_minor=row.amount_cents,
        currency=row.currency,
        stripe_price_id=row.stripe_price_id or pack.stripe_price_id,
        is_fallback=is_fallback,
    )


def _converts(currency: Currency) -> bool:
    return currency.rate_from_usd is not None


def _converted(base: ResolvedPrice, target: Currency) -> ResolvedPrice:
    """`base` (the catalogue default) shown in a converted currency, net.

    A default price that is not in USD cannot convert; it is quoted as is and
    marked a fallback — the same quiet answer an unpriced market gets.
    """
    if base.currency.code != FALLBACK_CURRENCY_CODE:
        return ResolvedPrice(base.amount_minor, base.currency, "", is_fallback=True)
    try:
        tax: Decimal | None = tax_rate_for(target.market_country) if target.market_country else None
    except CurrencyNotOnSaleError:
        tax = None
    return ResolvedPrice(
        amount_minor=convert_net(base.amount_minor, source=base.currency, target=target),
        currency=target,
        stripe_price_id="",
        tax_rate=tax,
    )


def _legacy_plan_price(plan: Plan, *, cycle: str) -> ResolvedPrice:
    amount = {
        "annual": plan.price_annual_cents,
        "per_workspace": plan.price_per_workspace_cents,
    }.get(cycle, plan.price_monthly_cents)
    stripe_id = plan.stripe_price_id_annual if cycle == "annual" else plan.stripe_price_id_monthly
    return ResolvedPrice(
        amount_minor=amount,
        currency=_fallback_currency(plan.currency),
        stripe_price_id=stripe_id,
    )


def _fallback_currency(code: str) -> Currency:
    """The `Currency` row for a legacy column's three-letter code.

    Creates it rather than failing: these paths run during the migration
    window, and a checkout that 500s because nobody has seeded a currency row
    yet is a worse failure than one that quotes the price it always quoted.
    """
    currency, created = Currency.objects.get_or_create(
        code=(code or FALLBACK_CURRENCY_CODE).upper(),
        defaults={"name": code or FALLBACK_CURRENCY_CODE, "symbol": ""},
    )
    if created:
        logger.warning("created a Currency row on demand", extra={"code": currency.code})
    return currency


# -----------------------------------------------------------------------------
# Converted prices (TND, BUILD-PLAN L-4 amended 2026-11-01)
# -----------------------------------------------------------------------------
class CurrencyNotOnSaleError(Exception):
    """A converted price cannot be quoted: no rate set, or no tax rate in force.

    Refused rather than quoted at zero — a missing rate or VAT is an operator's
    to-do, and charging without it is the one outcome that cannot be undone.
    """


@dataclass(frozen=True)
class ConvertedQuote:
    """Everything a TND order freezes at creation, in the target's minor units."""

    rate_from_usd: Decimal
    net_minor: int
    vat_rate: Decimal
    vat_minor: int

    @property
    def total_minor(self) -> int:
        return self.net_minor + self.vat_minor


def tax_rate_for(country: str, *, on: dt.date | None = None) -> Decimal:
    """The newest rate for `country` in force on `on` (today by default)."""
    row = (
        TaxRate.objects.filter(country=country.upper(), valid_from__lte=on or timezone.localdate())
        .order_by("-valid_from")
        .first()
    )
    if row is None:
        raise CurrencyNotOnSaleError(f"no tax rate in force for {country}")
    return row.rate


def convert_net(usd_minor: int, *, source: Currency, target: Currency) -> int:
    """The USD price x the target's rate, rounded half up to `round_to_minor`."""
    if source.code != FALLBACK_CURRENCY_CODE:
        raise ValueError(f"only {FALLBACK_CURRENCY_CODE} prices convert, not {source.code}")
    if target.rate_from_usd is None:
        raise CurrencyNotOnSaleError(f"{target.code} has no rate set")
    major = Decimal(usd_minor) / 10**source.minor_units * target.rate_from_usd
    step = Decimal(target.round_to_minor)
    steps = (major * 10**target.minor_units / step).quantize(Decimal(1), ROUND_HALF_UP)
    return int(steps * step)


def converted_quote(
    usd_minor: int, *, source: Currency, target: Currency, country: str
) -> ConvertedQuote:
    """The USD price x the target's rate, rounded half up to `round_to_minor`,
    plus VAT rounded half up to the minor unit."""
    net_minor = convert_net(usd_minor, source=source, target=target)
    vat_rate = tax_rate_for(country)
    vat_minor = int((net_minor * vat_rate).quantize(Decimal(1), ROUND_HALF_UP))
    assert target.rate_from_usd is not None  # convert_net refused otherwise
    return ConvertedQuote(
        rate_from_usd=target.rate_from_usd,
        net_minor=net_minor,
        vat_rate=vat_rate,
        vat_minor=vat_minor,
    )


# -----------------------------------------------------------------------------
# What is on sale right now (buy on the fly)
# -----------------------------------------------------------------------------
def purchase_options(workspace: Any, *, kind: str) -> list[dict[str, Any]]:
    """The packs that would unblock this request, priced for this customer.

    Attached to the 402 at the moment of the block so a stalled generation
    becomes a purchase instead of a dead end. Running out of credits does not
    need a different plan — it needs ten dollars — and routing that through a
    pricing page turns a thirty-second purchase into an abandoned session.

    Returns `[]` rather than raising when nothing is on sale: an exhausted
    allowance on a plan with no matching pack is a real state, and the surface
    should then offer the upgrade path alone rather than an empty buy button.
    """
    from billing.models import Pack

    organization = getattr(workspace, "organization", None)
    offers: list[dict[str, Any]] = []
    for pack in Pack.objects.filter(kind=kind, is_public=True).order_by("sort_order", "id"):
        resolved = pack_price(pack, organization=organization)
        offers.append(
            {
                "code": pack.code,
                "display_name": pack.display_name,
                "units": pack.units,
                "amount_minor": resolved.amount_minor,
                "currency": resolved.currency.code,
                # Rendered here for the same reason plan prices are: the
                # symbol, its position and the decimal count are per-currency
                # facts on the row, and duplicating that formatting in the
                # client puts ¥37.00 on screen the day Japan opens.
                "display": resolved.display,
            }
        )
    return offers
