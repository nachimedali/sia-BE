"""TN-03…TN-07, TN-13: TND prepaid periods through Konnect (`payments-tunisia.md`).

The rule every test here leans on: **only a payment read back from the gateway,
ours, for the frozen amount, grants anything.** The notification, the browser
and the success page grant nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import (
    CreditLedger,
    CreditReason,
    PrepaidKind,
    PrepaidOrder,
    PrepaidState,
    Subscription,
    SubscriptionStatus,
)
from billing.services import prepaid
from billing.services.flags import TN_PAYMENTS_V1

pytestmark = pytest.mark.django_db

TN = {"HTTP_X_COUNTRY": "TN"}


def _subscribe(client: APIClient, plan: str = "essential", **extra: Any) -> Any:
    return client.post(
        "/api/v1/billing/subscribe/",
        {"plan_code": plan, "buyer_name": "Acme SARL", "buyer_tax_id": "1234567A", **extra},
        format="json",
        HTTP_X_COUNTRY="TN",
    )


def _ref(order: PrepaidOrder) -> str:
    assert order.gateway_ref is not None
    return order.gateway_ref


def _sub(organization: Any) -> Subscription:
    sub = prepaid.current(organization)
    assert sub is not None
    return sub


def _end(organization: Any) -> dt.datetime:
    end = _sub(organization).period_end
    assert end is not None
    return end


def _notify(ref: str | None) -> Any:
    return APIClient().get("/api/v1/billing/webhook/konnect/", {"payment_ref": ref or ""})


def _pay(client: APIClient, konnect: Any, plan: str = "essential", **extra: Any) -> PrepaidOrder:
    assert _subscribe(client, plan, **extra).status_code == 200
    order = PrepaidOrder.objects.latest("pk")
    konnect.complete(_ref(order))
    _notify(order.gateway_ref)
    order.refresh_from_db()
    return order


# -----------------------------------------------------------------------------
# Which gateway, which currency (TN-13)
# -----------------------------------------------------------------------------
def test_a_visitor_from_tunisia_is_locked_to_tnd_and_sent_to_konnect(
    workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any, plans: dict[str, Any]
) -> None:
    organization = workspace.organization
    organization.trial_ends_at = timezone.now() - dt.timedelta(days=30)
    organization.save()

    response = _subscribe(auth_client)

    assert response.status_code == 200, response.data
    assert response.data["checkout_url"].startswith("https://pay.test/")
    organization.refresh_from_db()
    assert organization.billing_currency == tnd
    order = PrepaidOrder.objects.get()
    usd = plans["essential"].price_monthly_cents
    # $49 x 3 = 147 DT net, rounded to the dinar; 19% VAT; the month's stamp.
    assert order.net_minor == round(usd / 100 * 3) * 1000
    assert order.vat_minor == round(order.net_minor * 0.19)
    assert order.stamp_minor == 1000
    assert order.amount_minor == order.net_minor + order.vat_minor + 1000
    assert konnect.created[0]["amount_minor"] == order.amount_minor
    assert konnect.created[0]["currency"] == "TND"
    assert konnect.created[0]["webhook_url"].endswith("/api/v1/billing/webhook/konnect/")
    assert (order.buyer_name, order.buyer_tax_id) == ("Acme SARL", "1234567A")
    assert order.state == PrepaidState.PENDING


def test_everyone_else_is_locked_to_usd_and_stays_on_stripe(
    workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any
) -> None:
    from billing.gateways.fake import _fake_gateway

    response = auth_client.post(
        "/api/v1/billing/subscribe/", {"plan_code": "essential"}, format="json", HTTP_X_COUNTRY="FR"
    )

    workspace.organization.refresh_from_db()
    assert workspace.organization.billing_currency.code == "USD"
    assert konnect.created == []
    # Stripe was asked, or refused for a missing price id — either way not Konnect.
    assert response.status_code in (200, 400)
    assert _fake_gateway.checkout_calls or response.data["error"]["code"] == "plan_not_purchasable"


def test_a_company_already_on_stripe_is_not_moved_to_konnect(
    workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any
) -> None:
    organization = workspace.organization
    organization.stripe_customer_id = "cus_123"
    organization.save()

    _subscribe(auth_client)

    organization.refresh_from_db()
    assert organization.billing_currency.code == "USD"
    assert konnect.created == []


def test_with_the_flag_off_nothing_changes(
    workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any
) -> None:
    from billing.models import FeatureFlag

    FeatureFlag.objects.filter(key=TN_PAYMENTS_V1).update(enabled=False)

    _subscribe(auth_client)

    workspace.organization.refresh_from_db()
    assert workspace.organization.billing_currency is None
    assert konnect.created == []
    for path in ("prepaid/", "invoices/", "orders/1/"):
        assert auth_client.get(f"/api/v1/billing/{path}").status_code == 404, path


def test_the_pricing_page_shows_tnd_net_with_its_vat_to_a_visitor_from_tunisia(
    tnd: Any, plans: dict[str, Any]
) -> None:
    rows = APIClient().get("/api/v1/billing/plans/", HTTP_X_COUNTRY="TN").data
    essential = next(row for row in rows if row["code"] == "essential")

    assert essential["price"]["currency"] == "TND"
    assert (
        essential["price"]["amount_minor"]
        == round(plans["essential"].price_monthly_cents / 100 * 3) * 1000
    )
    assert essential["price"]["display"].endswith(" DT")
    assert essential["price"]["tax_rate"] == "0.1900"

    elsewhere = APIClient().get("/api/v1/billing/plans/", HTTP_X_COUNTRY="DE").data
    assert next(r for r in elsewhere if r["code"] == "essential")["price"]["currency"] == "USD"


def test_tnd_without_a_rate_is_not_on_sale(
    tn_workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any
) -> None:
    tnd.rate_from_usd = None
    tnd.save()

    response = _subscribe(auth_client)

    assert response.status_code == 400
    assert response.data["error"]["code"] == "currency_not_on_sale"
    assert konnect.created == []


def test_a_gateway_that_refuses_is_a_422_and_the_order_fails(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    konnect.fail_create = True

    response = _subscribe(auth_client)

    assert response.status_code == 422
    assert response.data["error"]["code"] == "gateway_unavailable"
    assert PrepaidOrder.objects.get().state == PrepaidState.FAILED


# -----------------------------------------------------------------------------
# Settling (TN-04): the only path that grants
# -----------------------------------------------------------------------------
def test_a_verified_payment_opens_a_month_and_grants_the_allowance_once(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, plans: dict[str, Any]
) -> None:
    order = _pay(auth_client, konnect)

    assert order.state == PrepaidState.PAID
    sub = _sub(tn_workspace.organization)
    assert sub.status == SubscriptionStatus.ACTIVE and sub.gateway == "konnect"
    assert sub.plan.code == "essential"
    assert _end(tn_workspace.organization) - timezone.now() > dt.timedelta(days=27)
    tn_workspace.organization.refresh_from_db()
    assert tn_workspace.organization.plan.code == "essential"
    grants = CreditLedger.objects.filter(
        workspace=tn_workspace, reason=CreditReason.MONTHLY_GRANT, note="essential allowance"
    )
    assert grants.count() == 1

    _notify(order.gateway_ref)  # a replay

    assert grants.count() == 1
    assert _end(tn_workspace.organization) == sub.period_end


def test_the_notification_redirects_the_payer_to_the_order_state_page(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()
    konnect.complete(_ref(order))

    response = _notify(order.gateway_ref)

    assert response.status_code == 302
    assert response["Location"].endswith(f"/app/billing/return?order={order.pk}")
    body = auth_client.get(f"/api/v1/billing/orders/{order.pk}/").data
    assert body["state"] == "PAID" and body["total"].endswith(" DT")


def test_an_unknown_reference_grants_nothing(tn_workspace: Any, konnect: Any) -> None:
    response = _notify("someone-elses-payment")

    assert response.status_code == 302
    assert prepaid.current(tn_workspace.organization) is None


def test_the_notification_alone_grants_nothing_while_the_payment_is_pending(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()

    _notify(order.gateway_ref)

    order.refresh_from_db()
    assert order.state == PrepaidState.PENDING
    assert prepaid.current(tn_workspace.organization) is None


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"amount_minor": 1}, "amount"),
        ({"currency": "EUR"}, "currency"),
        ({"order_id": "999"}, "order"),
        ({"wallet_id": "not-ours"}, "wallet"),
    ],
)
def test_a_payment_that_does_not_match_its_order_grants_nothing(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, settings: Any, change: Any, field: str
) -> None:
    settings.KONNECT_WALLET_ID = "our-wallet"
    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()
    konnect.complete(_ref(order), **change)

    _notify(order.gateway_ref)

    order.refresh_from_db()
    assert order.state == PrepaidState.MISMATCH
    assert field in order.note
    assert prepaid.current(tn_workspace.organization) is None


def test_a_failed_payment_is_recorded_and_grants_nothing(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()
    konnect.complete(_ref(order), state="failed")

    _notify(order.gateway_ref)

    order.refresh_from_db()
    assert order.state == PrepaidState.FAILED
    assert prepaid.current(tn_workspace.organization) is None


def test_a_lost_notification_is_recovered_by_the_sweep_and_a_dead_page_expires(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _subscribe(auth_client)
    _subscribe(auth_client)
    paid, abandoned = PrepaidOrder.objects.order_by("pk")
    konnect.complete(_ref(paid))
    PrepaidOrder.objects.filter(pk=paid.pk).update(
        created_at=timezone.now() - dt.timedelta(minutes=20)
    )
    PrepaidOrder.objects.filter(pk=abandoned.pk).update(
        created_at=timezone.now() - dt.timedelta(days=1)
    )

    assert prepaid.reread_pending() == 2

    paid.refresh_from_db()
    abandoned.refresh_from_db()
    assert paid.state == PrepaidState.PAID
    assert abandoned.state == PrepaidState.EXPIRED


def test_an_unreadable_gateway_leaves_the_order_for_the_sweep(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()
    konnect.complete(_ref(order))
    konnect.fail_fetch = True

    response = _notify(order.gateway_ref)

    assert response.status_code == 302
    order.refresh_from_db()
    assert order.state == PrepaidState.PENDING


def test_another_organizations_order_is_404(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, other_user: Any, tnd: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    _subscribe(auth_client)
    order = PrepaidOrder.objects.get()
    provision_workspace(other_user, name="Rival")
    rival = APIClient()
    rival.force_authenticate(other_user)

    assert rival.get(f"/api/v1/billing/orders/{order.pk}/").status_code == 404


def test_the_audit_reports_a_paid_order_the_gateway_no_longer_calls_paid(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    order = _pay(auth_client, konnect)
    konnect.complete(_ref(order), state="pending")

    findings = prepaid.audit_paid()

    assert findings == [{"order_id": order.pk, "problem": "gateway now says pending"}]
    order.refresh_from_db()
    assert order.state == PrepaidState.PAID  # reports, never repairs


# -----------------------------------------------------------------------------
# Periods (TN-05)
# -----------------------------------------------------------------------------
def test_renewing_early_adds_the_month_to_the_end_without_a_second_grant(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _pay(auth_client, konnect)
    first_end = _end(tn_workspace.organization)
    grants = CreditLedger.objects.filter(workspace=tn_workspace, reason=CreditReason.MONTHLY_GRANT)
    before = grants.count()

    renewal = _pay(auth_client, konnect)

    assert renewal.stamp_minor == 0  # the month's stamp was on the first order
    assert _end(tn_workspace.organization) > first_end + dt.timedelta(days=27)
    assert grants.count() == before


def test_a_year_is_twelve_months_at_the_annual_price(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, plans: dict[str, Any]
) -> None:
    order = _pay(auth_client, konnect, cycle="annual")

    assert order.months == 12
    assert order.usd_amount_minor == plans["essential"].price_annual_cents
    assert _end(tn_workspace.organization) - timezone.now() > dt.timedelta(days=360)


def test_changing_plan_mid_period_carries_the_days_left_at_the_price_ratio(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, plans: dict[str, Any]
) -> None:
    _pay(auth_client, konnect, "essential")
    sub = _sub(tn_workspace.organization)
    Subscription.objects.filter(pk=sub.pk).update(period_end=timezone.now() + dt.timedelta(days=10))

    _pay(auth_client, konnect, "growth")

    sub = _sub(tn_workspace.organization)
    ratio = plans["essential"].price_monthly_cents / plans["growth"].price_monthly_cents
    carried = _end(tn_workspace.organization) - (timezone.now() + dt.timedelta(days=28))
    assert sub.plan.code == "growth"
    assert abs(carried.total_seconds() / 86_400 - 10 * ratio) < 3.5  # month lengths vary


def test_a_trial_starts_without_payment_and_ends_without_grace(
    workspace: Any, tnd: Any, auth_client: APIClient, konnect: Any, plans: dict[str, Any]
) -> None:
    workspace.organization.billing_currency = tnd
    workspace.organization.save()

    response = _subscribe(auth_client)

    assert response.status_code == 200
    assert konnect.created == []
    sub = _sub(workspace.organization)
    assert sub.status == SubscriptionStatus.TRIALING
    later = _end(workspace.organization) + dt.timedelta(hours=1)
    assert prepaid.run_lifecycle(now=later)["lapsed"] == 1
    workspace.organization.refresh_from_db()
    assert workspace.organization.plan.is_default


def test_reminders_then_grace_then_the_default_plan(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    from notifications.models import Notification

    _pay(auth_client, konnect)
    sub = _sub(tn_workspace.organization)
    end = _end(tn_workspace.organization)

    assert prepaid.run_lifecycle(now=end - dt.timedelta(days=6, hours=12))["reminded"] == 1
    assert Notification.objects.filter(event_key="billing.renewal_due").exists()
    assert prepaid.run_lifecycle(now=end - dt.timedelta(days=3))["reminded"] == 0

    assert prepaid.run_lifecycle(now=end + dt.timedelta(days=1))["past_due"] == 1
    tn_workspace.organization.refresh_from_db()
    assert tn_workspace.organization.plan.code == "essential"  # still live in grace

    grace = sub.plan.prepaid_grace_days
    assert prepaid.run_lifecycle(now=end + dt.timedelta(days=grace, hours=1))["lapsed"] == 1
    tn_workspace.organization.refresh_from_db()
    assert tn_workspace.organization.plan.is_default


def test_paying_after_a_lapse_starts_a_fresh_period_with_a_fresh_allowance(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _pay(auth_client, konnect)
    prepaid.run_lifecycle(now=_end(tn_workspace.organization) + dt.timedelta(days=30))
    grants = CreditLedger.objects.filter(workspace=tn_workspace, note="essential allowance")
    before = grants.count()

    _pay(auth_client, konnect)

    assert _sub(tn_workspace.organization).status == SubscriptionStatus.ACTIVE
    assert grants.count() == before + 1


# -----------------------------------------------------------------------------
# Packs and extra workspaces (TN-06)
# -----------------------------------------------------------------------------
def test_a_pack_is_paid_through_konnect_and_credited_once_verified(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    from billing.models import Pack

    pack = Pack.objects.filter(is_public=True, kind="CREDITS").first() or Pack.objects.create(
        code="credits-test", display_name="Credits", kind="CREDITS", units=100, price_cents=1000
    )
    response = auth_client.post(
        "/api/v1/billing/purchase/", {"pack_code": pack.code}, format="json", HTTP_X_COUNTRY="TN"
    )
    assert response.status_code == 200, response.data
    order = PrepaidOrder.objects.get()
    assert order.kind == PrepaidKind.PACK
    purchased = CreditLedger.objects.filter(workspace=tn_workspace, reason=CreditReason.PURCHASE)
    assert purchased.count() == 0

    konnect.complete(_ref(order))
    _notify(order.gateway_ref)

    assert purchased.count() == 1


def test_an_extra_workspace_stays_pending_until_its_days_are_paid(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, user: Any
) -> None:
    from billing.services import subscriptions
    from workspaces.models import Workspace, WorkspaceStatus
    from workspaces.services.provisioning import provision_extra_workspace

    _pay(auth_client, konnect)
    organization = tn_workspace.organization
    cap = organization.plan.max_workspaces
    extra = None
    for n in range(cap):
        extra = provision_extra_workspace(user=user, name=f"Brand {n}")
        subscriptions.sync_workspace_quantity(organization)
    assert extra is not None
    extra.refresh_from_db()
    assert extra.status == WorkspaceStatus.PENDING_BILLING

    auth_client.credentials(HTTP_X_WORKSPACE_ID=str(tn_workspace.pk))
    summary = auth_client.get("/api/v1/billing/prepaid/").data
    assert summary["topup"]["workspaces"] == 1
    response = auth_client.post("/api/v1/billing/topup/", {}, format="json")
    assert response.status_code == 200, response.data
    order = PrepaidOrder.objects.filter(kind=PrepaidKind.WORKSPACE_TOPUP).get()
    konnect.complete(_ref(order))
    _notify(order.gateway_ref)

    assert Workspace.objects.get(pk=extra.pk).status == WorkspaceStatus.ACTIVE
    assert auth_client.get("/api/v1/billing/prepaid/").data["topup"] is None
    assert auth_client.post("/api/v1/billing/topup/", {}, format="json").status_code == 409


def test_the_summary_is_404_for_an_organization_not_on_konnect(
    workspace: Any, tnd: Any, auth_client: APIClient
) -> None:
    assert auth_client.get("/api/v1/billing/prepaid/").status_code == 404
