"""Which currency a visitor sees and an organization pays in (TN-13).

Owner, 2026-11-01: a visitor connected from Tunisia sees TND, everyone else
USD. The country comes from one request header, set by nginx from GeoIP and
forwarded by the BFF — never from a field the client fills in. **The currency
is locked on the organization at its first checkout** (one company, one
currency, one invoice, L-1); after that the connection country no longer
matters, so a customer travelling keeps their dinars. Only staff change it.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from billing.models import Currency
from billing.services import flags, pricing


def request_country(request: Any) -> str:
    """The two-letter country of the connection, or `""` when unknown."""
    value = str(request.META.get(settings.COUNTRY_HEADER, "") if request is not None else "")
    value = value.strip().upper()
    return value if len(value) == 2 and value.isalpha() else ""


def market_currency(organization: Any, country: str) -> Currency | None:
    """The organization's locked currency, else the one its country is shown.

    `None` means the catalogue default (USD). The country only counts while
    the TN flag is on for this organization (or globally, for a visitor).
    """
    if organization is not None and organization.billing_currency_id:
        currency: Currency = organization.billing_currency
        return currency
    if not country or not flags.flag_enabled(organization, flags.TN_PAYMENTS_V1):
        return None
    return Currency.objects.filter(market_country=country, is_active=True).first()


def lock_currency(organization: Any, country: str) -> Currency:
    """Fixes the organization's currency at its first checkout and returns it.

    An organization that already has a Stripe customer stays on Stripe's
    currency: moving a company with a live card subscription to Konnect would
    bill it twice.
    """
    if organization.billing_currency_id:
        locked: Currency = organization.billing_currency
        return locked
    currency = None if organization.stripe_customer_id else market_currency(organization, country)
    if currency is None:
        currency = pricing._fallback_currency(pricing.FALLBACK_CURRENCY_CODE)
    organization.billing_currency = currency
    organization.save(update_fields=["billing_currency", "updated_at"])
    return currency


def uses_prepaid(organization: Any) -> bool:
    """Whether this organization pays through Konnect rather than Stripe."""
    currency = getattr(organization, "billing_currency", None)
    return (
        currency is not None
        and currency.gateway == "konnect"
        and flags.flag_enabled(organization, flags.TN_PAYMENTS_V1)
    )
