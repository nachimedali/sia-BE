"""TN-12, TN-14: monthly French invoices for TND accounts, Stripe's for the rest."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import Invoice, InvoiceIssuer, PrepaidOrder
from billing.rendering import WeasyPrintRenderer
from billing.services import invoicing
from billing.services.prepaid import month_start
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
    assert f"Réf. Konnect {first.gateway_ref}" in html
    assert "Arrêtée la présente facture à la somme de" in html
    assert invoicing.fr_money(invoice.currency, invoice.total_minor) in html
    assert invoice.sha256 == hashlib.sha256(invoice.document.open("rb").read()).hexdigest()

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

    note = invoicing.issue_credit_note(invoice, reason="Prélevé deux fois")
    # The stamp duty is not refunded: the note returns net + VAT only.
    assert note.stamp_minor == 0
    assert note.total_minor == -(invoice.net_minor + invoice.vat_minor)
    assert note.corrects == invoice
    assert note.reason == "Prélevé deux fois"
    assert note.number.endswith("-00002")
    html = note.document.read().decode()
    assert "AVOIR" in html and f"Avoir sur la facture n° {invoice.number}" in html
    assert "Total TTC à rembourser" in html and "Prélevé deux fois" in html
    with pytest.raises(StateConflict):
        invoicing.issue_credit_note(invoice, reason="again")


def test_the_invoice_list_and_download_are_the_organizations_own(
    tn_workspace: Any, auth_client: APIClient, konnect: Any, issuer: InvoiceIssuer, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    _paid_order(auth_client, konnect, on=dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    invoicing.issue_due(now=NOVEMBER)
    invoice = Invoice.objects.get()

    body = auth_client.get("/api/v1/billing/invoices/").data
    assert body["issued_by"] == "composevision" and body["late"] is None
    rows = body["invoices"]
    assert [(r["origin"], r["number"], r["is_credit_note"]) for r in rows] == [
        ("composevision", invoice.number, False)
    ]
    assert rows[0]["total"].endswith(" DT")
    assert rows[0]["minor_units"] == 3 and rows[0]["lines"] == 1
    assert rows[0]["charged_minor"] == invoice.total_minor  # what Konnect took that month
    assert rows[0]["sha256"] == invoice.sha256
    download = auth_client.get(f"/api/v1/billing/invoices/{invoice.pk}/document/")
    assert download.status_code == 200
    assert b"FACTURE" in b"".join(download.streaming_content)  # type: ignore[attr-defined]

    provision_workspace(other_user, name="Rival")
    rival = APIClient()
    rival.force_authenticate(other_user)
    assert rival.get(f"/api/v1/billing/invoices/{invoice.pk}/document/").status_code == 404
    assert rival.get("/api/v1/billing/invoices/").data["invoices"] == []


def test_a_month_held_back_by_an_incomplete_issuer_is_reported_late(
    tn_workspace: Any, auth_client: APIClient, konnect: Any
) -> None:
    paid = _paid_order(auth_client, konnect, on=timezone.now() - dt.timedelta(days=40))

    body = auth_client.get("/api/v1/billing/invoices/").data

    assert body["invoices"] == []
    assert paid.paid_at is not None
    assert body["late"] == str(month_start(paid.paid_at).date())
    assert body["next_issue_on"] == str(
        (month_start(timezone.now()) + dt.timedelta(days=32)).date().replace(day=1)
    )


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

    body = auth_client.get("/api/v1/billing/invoices/").data

    assert body["issued_by"] == "stripe" and body["next_issue_on"] is None
    rows = body["invoices"]
    assert rows[0]["origin"] == "stripe"
    assert rows[0]["pdf_url"] == "https://stripe.test/in_1.pdf"
    assert rows[0]["total"] == "$49.00"


@pytest.mark.parametrize(
    ("minor", "words"),
    [
        (279_000, "Deux cent soixante-dix-neuf dinars"),
        (1_500, "Un dinar et cinq cents millimes"),
        (71_000, "Soixante et onze dinars"),
        (80_000, "Quatre-vingts dinars"),
        (181_000, "Cent quatre-vingt-un dinars"),
        (200_000, "Deux cents dinars"),
        (1_000, "Un dinar"),
        (2_200_001, "Deux mille deux cents dinars et un millime"),
        (280_000_000, "Deux cent quatre-vingt mille dinars"),
        (0, "Zéro dinar"),
    ],
)
def test_the_total_is_spelled_out_in_french(tnd: Any, minor: int, words: str) -> None:
    assert invoicing.amount_in_words(tnd, minor) == words


def test_amounts_print_the_french_way(tnd: Any) -> None:
    assert invoicing.fr_money(tnd, 1_234_500) == "1\u00a0234,500 DT"


def test_weasyprint_renders_the_template_to_a_real_pdf(issuer: InvoiceIssuer, tnd: Any) -> None:
    pytest.importorskip("weasyprint")
    from django.template.loader import render_to_string

    html = render_to_string(
        "billing/invoice.html",
        invoicing.context(
            kind="invoice",
            number="CV-2026-00001",
            issued_on=dt.date(2026, 11, 1),
            period=dt.date(2026, 10, 1),
            issuer={"legal_name": "X", "address": "Y", "tax_id": "Z", "trade_register": "R"},
            buyer_name="Acme",
            buyer_tax_id="",
            currency=tnd,
            lines=[],
        ),
    )
    assert WeasyPrintRenderer().render(html).startswith(b"%PDF")
