"""Prepaid periods, packs and extra workspaces paid in TND (TN-04…TN-07).

Konnect sells one-off payments only, so a TND plan is a **prepaid period**: the
customer pays 1 or 12 months and renews by hand; nothing renews by itself
(`payments-tunisia.md` §5). One `Subscription` row per organization,
`prepaid:<organization id>`, is extended by each paid `PrepaidOrder` and read
by the entitlement resolver exactly as a Stripe row is.

**Nothing is granted by a request.** An order grants only when `settle` has
read the payment back from the gateway and found it completed, for the amount,
currency and order this row froze, into our wallet. The gateway's unsigned
notification only tells us which reference to read.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.gateways.base import BillingGatewayError, CheckoutSession
from billing.gateways.prepaid import get_prepaid_gateway
from billing.models import (
    UNLIMITED,
    Currency,
    Pack,
    Plan,
    PrepaidKind,
    PrepaidOrder,
    PrepaidState,
    Subscription,
    SubscriptionStatus,
    TaxStamp,
)
from billing.services import pricing, subscriptions
from common.exceptions import OCCSError, ProviderError, StateConflict
from workspaces.models import Workspace

logger = logging.getLogger(__name__)

#: Invoices, the stamp's "first order of the month" and the month boundary all
#: follow the Tunisian company's calendar.
TUNIS = ZoneInfo("Africa/Tunis")
#: A pending order is re-read after this long, in case its notification was lost.
REREAD_AFTER = dt.timedelta(minutes=15)


def subscription_id(organization: Any) -> str:
    return f"prepaid:{organization.pk}"


def current(organization: Any) -> Subscription | None:
    return (
        Subscription.objects.filter(stripe_subscription_id=subscription_id(organization))
        .select_related("plan")
        .first()
    )


def month_start(moment: dt.datetime) -> dt.datetime:
    local = moment.astimezone(TUNIS)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _webhook_url() -> str:
    base = (settings.API_PUBLIC_URL or settings.SITE_URL).rstrip("/")
    return f"{base}/api/v1/billing/webhook/konnect/"


def _stamp_for(organization: Any, currency: Currency, now: dt.datetime) -> int:
    """The stamp duty, carried by the month's first paid order only (D-5)."""
    if not currency.market_country:
        return 0
    already = PrepaidOrder.objects.filter(
        organization=organization,
        state=PrepaidState.PAID,
        stamp_minor__gt=0,
        paid_at__gte=month_start(now),
    ).exists()
    if already:
        return 0
    stamp = (
        TaxStamp.objects.filter(country=currency.market_country, valid_from__lte=now.date())
        .order_by("-valid_from")
        .first()
    )
    return stamp.amount_minor if stamp else 0


def _usd(amount: pricing.ResolvedPrice) -> int:
    if amount.currency.code != pricing.FALLBACK_CURRENCY_CODE:
        raise OCCSError("This item is not available for purchase yet.", code="not_purchasable")
    return amount.amount_minor


# -----------------------------------------------------------------------------
# Orders
# -----------------------------------------------------------------------------
def _create_order(
    workspace: Workspace,
    *,
    user: Any,
    kind: str,
    usd_minor: int,
    description: str,
    buyer_name: str = "",
    buyer_tax_id: str = "",
    plan: Plan | None = None,
    months: int = 0,
    pack: Pack | None = None,
    workspaces: int = 0,
) -> CheckoutSession:
    organization = workspace.organization
    currency = organization.billing_currency
    assert currency is not None, "only called once the currency is locked"
    gateway = get_prepaid_gateway()
    try:
        quote = pricing.converted_quote(
            usd_minor,
            source=pricing._fallback_currency(pricing.FALLBACK_CURRENCY_CODE),
            target=currency,
            country=currency.market_country,
        )
    except pricing.CurrencyNotOnSaleError as exc:
        logger.error("converted price unavailable", extra={"currency": currency.code})
        raise OCCSError(
            f"Payments in {currency.code} are not open yet.", code="currency_not_on_sale"
        ) from exc

    now = timezone.now()
    stamp = _stamp_for(organization, currency, now)
    order = PrepaidOrder.objects.create(
        organization=organization,
        workspace=workspace,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        kind=kind,
        plan=plan,
        months=months,
        pack=pack,
        workspaces=workspaces,
        currency=currency,
        usd_amount_minor=usd_minor,
        rate_from_usd=quote.rate_from_usd,
        net_minor=quote.net_minor,
        vat_rate=quote.vat_rate,
        vat_minor=quote.vat_minor,
        stamp_minor=stamp,
        amount_minor=quote.total_minor + stamp,
        buyer_name=buyer_name.strip()[:160],
        buyer_tax_id=buyer_tax_id.strip()[:32],
        gateway_key=gateway.key,
    )
    try:
        hosted = gateway.create_payment(
            order_id=str(order.pk),
            amount_minor=order.amount_minor,
            currency=currency.code,
            description=description,
            email=organization.owner.email,
            webhook_url=_webhook_url(),
        )
    except BillingGatewayError as exc:
        order.state = PrepaidState.FAILED
        order.note = str(exc)[:280]
        order.save(update_fields=["state", "note", "updated_at"])
        logger.warning("payment page refused", extra={"order_id": order.pk}, exc_info=True)
        raise ProviderError(
            "The payment page could not be opened. Try again in a moment.",
            code="gateway_unavailable",
        ) from exc

    order.gateway_ref = hosted.ref
    order.pay_url = hosted.url
    order.save(update_fields=["gateway_ref", "pay_url", "updated_at"])
    logger.info(
        "prepaid order created",
        extra={"order_id": order.pk, "kind": kind, "amount_minor": order.amount_minor},
    )
    return CheckoutSession(id=str(order.pk), url=hosted.url)


def _overage_for(organization: Any, plan: Plan) -> int:
    """Workspaces beyond what `plan` includes, as a period for it would bill."""
    if plan.max_workspaces == UNLIMITED:
        return 0
    return max(0, subscriptions.live_workspace_count(organization) - int(plan.max_workspaces))


def start_plan(
    workspace: Workspace,
    *,
    plan: Plan,
    cycle: str,
    user: Any = None,
    buyer_name: str = "",
    buyer_tax_id: str = "",
) -> CheckoutSession:
    """A period of `plan`: the trial if this organization never had one, else
    an order for 1 month (monthly) or 12 months at the annual price."""
    organization = workspace.organization
    sub = current(organization)
    if plan.trial_days and organization.trial_ends_at is None and not (sub and sub.is_live):
        now = timezone.now()
        trial = subscriptions.apply_subscription_state(
            workspace,
            plan=plan,
            stripe_subscription_id=subscription_id(organization),
            status=SubscriptionStatus.TRIALING,
            period_start=now,
            period_end=now + dt.timedelta(days=plan.trial_days),
            cancel_at_period_end=True,
        )
        Subscription.objects.filter(pk=trial.pk).update(gateway="konnect")
        return CheckoutSession(
            id=trial.stripe_subscription_id, url=f"{settings.SITE_URL}/app/billing?checkout=success"
        )

    annual = cycle == "annual"
    extra = _overage_for(organization, plan)
    per_workspace = _usd(pricing.plan_per_workspace_price(plan)) * (10 if annual else 1)
    usd = _usd(pricing.plan_price(plan, cycle=cycle)) + extra * per_workspace
    months = 12 if annual else 1
    return _create_order(
        workspace,
        user=user,
        kind=PrepaidKind.PLAN_PERIOD,
        usd_minor=usd,
        plan=plan,
        months=months,
        workspaces=extra,
        description=f"ComposeVision {plan.display_name}, {months} mois",
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
    )


def start_pack(
    workspace: Workspace,
    *,
    pack: Pack,
    user: Any = None,
    buyer_name: str = "",
    buyer_tax_id: str = "",
) -> CheckoutSession:
    return _create_order(
        workspace,
        user=user,
        kind=PrepaidKind.PACK,
        usd_minor=_usd(pricing.pack_price(pack)),
        pack=pack,
        description=f"ComposeVision {pack.display_name}",
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
    )


def topup_quote(organization: Any, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """What the extra workspaces not yet paid for cost until the period ends.

    Pro rata by days left over a 30-day month, rounded up to whole days. `None`
    when nothing is owed or there is no live paid period to add them to.
    """
    now = now or timezone.now()
    sub = current(organization)
    if sub is None or not sub.is_live or sub.status == SubscriptionStatus.TRIALING:
        return None
    if sub.period_end is None or sub.period_end <= now:
        return None
    needed = subscriptions.billable_overage(organization) - sub.prepaid_workspaces
    if needed <= 0:
        return None
    days_left = math.ceil((sub.period_end - now).total_seconds() / 86_400)
    per_workspace = _usd(pricing.plan_per_workspace_price(sub.plan))
    usd = math.ceil(needed * per_workspace * days_left / 30)
    shown = pricing.convert_net(
        usd,
        source=pricing._fallback_currency(pricing.FALLBACK_CURRENCY_CODE),
        target=organization.billing_currency,
    )
    return {
        "workspaces": needed,
        "days_left": days_left,
        "usd_amount_minor": usd,
        "net_minor": shown,
        "display": organization.billing_currency.format(shown),
    }


def start_topup(
    workspace: Workspace, *, user: Any = None, buyer_name: str = "", buyer_tax_id: str = ""
) -> CheckoutSession:
    quote = topup_quote(workspace.organization)
    if quote is None:
        raise StateConflict("There are no extra workspaces to pay for.", code="nothing_to_pay")
    return _create_order(
        workspace,
        user=user,
        kind=PrepaidKind.WORKSPACE_TOPUP,
        usd_minor=quote["usd_amount_minor"],
        workspaces=quote["workspaces"],
        description=f"ComposeVision, {quote['workspaces']} espace(s) de travail supplémentaire(s)",
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
    )


# -----------------------------------------------------------------------------
# Settling: the only path that grants
# -----------------------------------------------------------------------------
def settle(ref: str) -> PrepaidOrder | None:
    """Reads payment `ref` back and, if it is ours and paid in full, fulfils it.

    Only references we created are looked up — a reference to someone else's
    payment, however well formed, finds no order. Idempotent: a paid order is
    left alone. The gateway is read **before** the row lock is taken, so a
    slow gateway never holds a lock (the rule the 20-way debit test guards).
    Raises `BillingGatewayError` when the gateway cannot be read; the order
    stays as it was and the sweep reads it again.
    """
    if not PrepaidOrder.objects.filter(gateway_ref=ref).exists():
        logger.warning("payment notification for an unknown reference", extra={"ref": ref[:64]})
        return None
    status = get_prepaid_gateway().fetch_payment(ref)

    with transaction.atomic():
        order = (
            PrepaidOrder.objects.select_for_update().select_related("currency").get(gateway_ref=ref)
        )
        if order.state in (PrepaidState.PAID, PrepaidState.MISMATCH):
            return order
        if status.state == "pending":
            return order
        if status.state == "failed":
            order.state = PrepaidState.FAILED
            order.save(update_fields=["state", "updated_at"])
            return order

        mismatch = [
            name
            for name, ok in (
                ("amount", status.amount_minor == order.amount_minor),
                ("currency", status.currency == order.currency.code),
                ("order", status.order_id == str(order.pk)),
                (
                    "wallet",
                    not status.wallet_id
                    or not settings.KONNECT_WALLET_ID
                    or status.wallet_id == settings.KONNECT_WALLET_ID,
                ),
            )
            if not ok
        ]
        if mismatch:
            order.state = PrepaidState.MISMATCH
            order.note = f"gateway disagrees on: {', '.join(mismatch)}"
            order.save(update_fields=["state", "note", "updated_at"])
            logger.error(
                "prepaid payment does not match its order; nothing granted",
                extra={"order_id": order.pk, "mismatch": mismatch},
            )
            return order

        order.state = PrepaidState.PAID
        order.paid_at = timezone.now()
        order.save(update_fields=["state", "paid_at", "updated_at"])
        _fulfil(order)
    logger.info("prepaid order paid", extra={"order_id": order.pk, "kind": order.kind})
    return order


def _fulfil(order: PrepaidOrder) -> None:
    if order.kind == PrepaidKind.PLAN_PERIOD:
        _extend(order)
    elif order.kind == PrepaidKind.PACK:
        from billing.services.purchases import fulfil_purchase

        assert order.pack is not None
        fulfil_purchase(order.workspace, pack_code=order.pack.code)
    else:
        sub = current(order.organization)
        if sub is None:
            raise OCCSError("A top-up was paid with no period to add it to.", code="no_period")
        Subscription.objects.filter(pk=sub.pk).update(
            prepaid_workspaces=sub.prepaid_workspaces + order.workspaces
        )
        subscriptions.sync_workspace_quantity(order.organization)


def _monthly_usd(plan: Plan) -> Decimal:
    return Decimal(max(1, pricing.plan_price(plan).amount_minor))


def _extend(order: PrepaidOrder) -> None:
    """Adds the paid months to the organization's period.

    Same plan, still live: the months go on the end, so paying early loses
    nothing. A different plan, still live: the new period starts now and the
    days left on the old one carry over at the two plans' price ratio. Lapsed,
    or never paid: it starts now. A trial being paid for starts now too, and
    keeps the trial days still to run.
    """
    assert order.plan is not None
    organization = order.organization
    plan = order.plan
    now = timezone.now()
    sub = current(organization)
    months = relativedelta(months=order.months)
    live = sub is not None and sub.is_live and sub.period_end is not None and sub.period_end > now

    if live and sub and sub.period_end and sub.status == SubscriptionStatus.TRIALING:
        start, end, fresh = now, sub.period_end + months, True
    elif live and sub and sub.period_end and sub.plan_id == plan.pk:
        start, end, fresh = sub.period_start or now, sub.period_end + months, False
    elif live and sub and sub.period_end:
        ratio = _monthly_usd(sub.plan) / _monthly_usd(plan)
        carried = (sub.period_end - now) * float(ratio)
        start, end, fresh = now, now + months + carried, True
    else:
        start, end, fresh = now, now + months, True

    created = sub is None
    applied = subscriptions.apply_subscription_state(
        order.workspace,
        plan=plan,
        stripe_subscription_id=subscription_id(organization),
        status=SubscriptionStatus.ACTIVE,
        period_start=start,
        period_end=end,
        cancel_at_period_end=True,
    )
    Subscription.objects.filter(pk=applied.pk).update(
        gateway="konnect", prepaid_workspaces=order.workspaces
    )
    if fresh and not created:
        subscriptions._grant_period_allowances(order.workspace, plan)
    subscriptions.sync_workspace_quantity(organization)


# -----------------------------------------------------------------------------
# Sweeps
# -----------------------------------------------------------------------------
def reread_pending(now: dt.datetime | None = None) -> int:
    """Re-reads unconfirmed orders (a lost notification), and expires the ones
    past their payment page's lifespan. Returns how many changed state."""
    now = now or timezone.now()
    lifespan = dt.timedelta(minutes=settings.PREPAID_PAYMENT_LIFESPAN_MINUTES + 60)
    changed = 0
    pending = PrepaidOrder.objects.filter(
        state=PrepaidState.PENDING,
        gateway_ref__isnull=False,
        created_at__lt=now - REREAD_AFTER,
    )
    for order in pending.iterator():
        try:
            settled = settle(order.gateway_ref or "")
        except BillingGatewayError:
            logger.warning("pending order could not be re-read", extra={"order_id": order.pk})
            continue
        if settled is not None and settled.state != PrepaidState.PENDING:
            changed += 1
        elif order.created_at < now - lifespan:
            PrepaidOrder.objects.filter(pk=order.pk, state=PrepaidState.PENDING).update(
                state=PrepaidState.EXPIRED, updated_at=now
            )
            changed += 1
    return changed


def audit_paid(now: dt.datetime | None = None, *, days: int = 2) -> list[dict[str, Any]]:
    """Nightly: every order paid in the last `days`, read back again.

    **Reports, never repairs** (Part 7 rule 7): a paid order the gateway no
    longer calls completed for the same amount is logged for a person — a
    refund, a chargeback, or a bug in the path that granted it.
    """
    now = now or timezone.now()
    findings: list[dict[str, Any]] = []
    paid = PrepaidOrder.objects.filter(
        state=PrepaidState.PAID, paid_at__gte=now - dt.timedelta(days=days)
    )
    for order in paid.iterator():
        try:
            status = get_prepaid_gateway().fetch_payment(order.gateway_ref or "")
        except BillingGatewayError:
            findings.append({"order_id": order.pk, "problem": "unreadable"})
            continue
        if status.state != "completed" or status.amount_minor != order.amount_minor:
            findings.append({"order_id": order.pk, "problem": f"gateway now says {status.state}"})
    for finding in findings:
        logger.error("prepaid audit finding", extra=finding)
    return findings


def run_lifecycle(now: dt.datetime | None = None) -> dict[str, int]:
    """Daily: reminds before a period ends, then grace, then the default plan.

    Reminders go out when 7 and 1 days are left (`PREPAID_REMINDER_DAYS`). A
    lapsed paid period stays live as `PAST_DUE` for the plan's grace days; a
    lapsed trial has none. Then `CANCELED`, and the existing downgrade runs —
    it marks, never deletes (I10).
    """
    from notifications.services import renewal_due_event

    now = now or timezone.now()
    counts = {"reminded": 0, "past_due": 0, "lapsed": 0}
    live = Subscription.objects.filter(
        gateway="konnect", status__in=SubscriptionStatus.live(), period_end__isnull=False
    ).select_related("plan", "workspace__organization")
    for sub in live.iterator():
        assert sub.period_end is not None
        if sub.period_end > now:
            days_left = math.ceil((sub.period_end - now).total_seconds() / 86_400)
            if days_left in settings.PREPAID_REMINDER_DAYS:
                renewal_due_event(sub.workspace, ends_at=sub.period_end, days_left=days_left)
                counts["reminded"] += 1
            continue
        grace = 0 if sub.status == SubscriptionStatus.TRIALING else sub.plan.prepaid_grace_days
        lapsed = now >= sub.period_end + dt.timedelta(days=grace)
        status = SubscriptionStatus.CANCELED if lapsed else SubscriptionStatus.PAST_DUE
        if status == sub.status:
            continue
        subscriptions.apply_subscription_state(
            sub.workspace,
            plan=sub.plan,
            stripe_subscription_id=sub.stripe_subscription_id,
            status=status,
            period_start=sub.period_start,
            period_end=sub.period_end,
            cancel_at_period_end=True,
        )
        counts["lapsed" if lapsed else "past_due"] += 1
    return counts


def summary(organization: Any) -> dict[str, Any]:
    """What the billing page shows a TND organization."""
    sub = current(organization)
    period = None
    if sub is not None:
        grace = sub.plan.prepaid_grace_days if sub.status != SubscriptionStatus.TRIALING else 0
        period = {
            "plan_code": sub.plan.code,
            "plan_name": sub.plan.display_name,
            "status": sub.status,
            "period_end": sub.period_end,
            "grace_until": sub.period_end + dt.timedelta(days=grace) if sub.period_end else None,
            "extra_workspaces": sub.prepaid_workspaces,
        }
    return {
        "currency": organization.billing_currency.code,
        "period": period,
        "topup": topup_quote(organization),
    }
