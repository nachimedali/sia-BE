"""TN-12, TN-14: monthly French invoices for TND accounts, Stripe's for the rest."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import Invoice, InvoiceIssuer, PrepaidOrder
from billing.rendering import WeasyPrintRenderer
from billing.services import invoicing
from common.records import AppendOnlyError

pytestmark = pytest.mark.django_db

TN = {"HTTP_X_COUNTRY": "TN"}
NOVEMBER = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def issuer(db: None) -> InvoiceIssuer:
    issuer = InvoiceIssuer.get()
    issuer.legal_name = "ComposeVision SUARL"
    issuer.address = "Rue du Lac, Tunis"
    issuer.tax_id = "1234567/A/M/000"
    issuer.trade_register = "B0123452026"
    issuer.save()
    return issuer


def _ref(order: PrepaidOrder) -> str:
    assert order.gateway_ref is not None
    return order.gateway_ref


def _paid_order(
    client: APIClient, konnect: Any, *, plan: str = "essential", on: dt.datetime
) -> PrepaidOrder:
    client.post(
        "/api/v1/billing/subscribe/",
        {"plan_code": plan, "buyer_name": "Acme SARL", "buyer_tax_id": "7654321B"},
        format="json",
        HTTP_X_COUNTRY="TN",
    )
    order = PrepaidOrder.objects.latest("pk")
    konnect.complete(_ref(order))
    APIClient().get("/api/v1/billing/webhook/konnect/", {"payment_ref": order.gateway_ref or ""})
    PrepaidOrder.objects.filter(pk=order.pk).update(paid_at=on)
    order.refresh_from_db()
    return order


def test_last_months_payments_become_one_invoice_whose_total_is_what_was_charged(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, issuer: InvoiceIssuer
) -> None:
    first = _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    second = _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 20, tzinfo=dt.UTC))
    _paid_order(auth_client, konnect, on=NOVEMBER)  # this month: not yet

    assert invoicing.issue_due(now=NOVEMBER + dt.timedelta(hours=1)) == 1

    invoice = Invoice.objects.get()
    assert invoice.period == dt.date(2026, 10, 1)
    assert invoice.number == f"CV-{timezone.now().year}-00001"
    assert invoice.total_minor == first.amount_minor + second.amount_minor
    assert invoice.stamp_minor == first.stamp_minor + second.stamp_minor  # as charged
    assert invoice.lines.count() == 2
    html = invoice.document.read().decode()
    assert "FACTURE" in html and "ComposeVision SUARL" in html and "7654321B" in html
    assert "Droit de timbre" in html and "Total TTC" in html
    assert "Abonnement" in html

    assert invoicing.issue_due(now=NOVEMBER + dt.timedelta(days=1)) == 0  # idempotent


def test_numbers_run_without_gaps_across_organizations(
    tn_workspace: Any,
    auth_client: APIClient,
    konnect: Any,
    issuer: InvoiceIssuer,
    other_user: Any,
    tnd: Any,
) -> None:
    from workspaces.services.provisioning import provision_workspace

    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    rival = provision_workspace(other_user, name="Rival")
    rival.organization.billing_currency = tnd
    rival.organization.trial_ends_at = timezone.now() - dt.timedelta(days=30)
    rival.organization.save()
    other = APIClient()
    other.force_authenticate(other_user)
    _paid_order(other, konnect, on=dt.datetime(2026, 10, 9, tzinfo=dt.UTC))

    assert invoicing.issue_due(now=NOVEMBER) == 2
    year = timezone.now().year
    assert sorted(Invoice.objects.values_list("number", flat=True)) == [
        f"CV-{year}-00001",
        f"CV-{year}-00002",
    ]


def test_nothing_is_issued_while_the_issuer_is_incomplete(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))

    assert invoicing.issue_due(now=NOVEMBER) == 0
    assert not Invoice.objects.exists()


def test_a_deployment_without_a_pdf_engine_issues_nothing(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, issuer: InvoiceIssuer, settings: Any
) -> None:
    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    settings.FAKE_BILLING_REFUSES = True

    assert invoicing.issue_due(now=NOVEMBER) == 0


def test_an_invoice_is_frozen_and_corrected_only_by_a_credit_note(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, issuer: InvoiceIssuer
) -> None:
    from common.exceptions import StateConflict

    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    invoicing.issue_due(now=NOVEMBER)
    invoice = Invoice.objects.get()

    invoice.buyer_name = "Edited"
    with pytest.raises(AppendOnlyError):
        invoice.save()

    note = invoicing.issue_credit_note(invoice, reason="Remboursé")
    assert note.total_minor == -invoice.total_minor
    assert note.corrects == invoice
    assert note.number.endswith("-00002")
    assert "AVOIR" in note.document.read().decode()
    with pytest.raises(StateConflict):
        invoicing.issue_credit_note(invoice, reason="again")


def test_the_invoice_list_and_download_are_the_organizations_own(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, issuer: InvoiceIssuer, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    invoicing.issue_due(now=NOVEMBER)
    invoice = Invoice.objects.get()

    rows = auth_client.get("/api/v1/billing/invoices/").data
    assert [(r["origin"], r["number"], r["is_credit_note"]) for r in rows] == [
        ("composevision", invoice.number, False)
    ]
    assert rows[0]["total"].endswith(" DT")
    download = auth_client.get(f"/api/v1/billing/invoices/{invoice.pk}/document/")
    assert download.status_code == 200
    assert b"FACTURE" in b"".join(download.streaming_content)  # type: ignore[attr-defined]

    provision_workspace(other_user, name="Rival")
    rival = APIClient()
    rival.force_authenticate(other_user)
    assert rival.get(f"/api/v1/billing/invoices/{invoice.pk}/document/").status_code == 404
    assert rival.get("/api/v1/billing/invoices/").data == []


def test_a_usd_account_lists_stripes_invoices(
    workspace: Any, tnd: Any, auth_client: APIClient
) -> None:
    from billing.gateways.base import GatewayInvoice
    from billing.gateways.fake import _fake_gateway

    workspace.organization.stripe_customer_id = "cus_1"
    workspace.organization.save()
    _fake_gateway.invoices.append(
        GatewayInvoice(
            "in_1", "A1-0001", 1_793_000_000, 4900, "USD", "https://stripe.test/in_1.pdf"
        )
    )

    rows = auth_client.get("/api/v1/billing/invoices/").data

    assert rows[0]["origin"] == "stripe"
    assert rows[0]["pdf_url"] == "https://stripe.test/in_1.pdf"
    assert rows[0]["total"] == "$49.00"


def test_weasyprint_renders_the_template_to_a_real_pdf(issuer: InvoiceIssuer) -> None:
    pytest.importorskip("weasyprint")
    from django.template.loader import render_to_string

    html = render_to_string(
        "billing/invoice.html",
        {
            "number": "CV-2026-00001",
            "issued_on": dt.date(2026, 11, 1),
            "period": dt.date(2026, 10, 1),
            "issuer": {"legal_name": "X", "address": "Y", "tax_id": "Z", "trade_register": "R"},
            "buyer_name": "Acme",
            "lines": [
                {"label": "Abonnement", "net": "147.000 DT", "vat": "27.930 DT", "rate": "19 %"}
            ],
            "net": "147.000 DT",
            "vat": "27.930 DT",
            "stamp": "1.000 DT",
            "total": "175.930 DT",
        },
    )
    assert WeasyPrintRenderer().render(html).startswith(b"%PDF")
