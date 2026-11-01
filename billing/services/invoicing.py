"""Monthly TND invoices (TN-12, `payments-tunisia.md` D-5).

For each organization, one invoice per month listing what it paid that month
(plan periods, packs, extra workspaces): net, 19% VAT, the stamp duty and the
total — which always equals what Konnect charged, because every one of those
amounts was frozen on the order when it was created. A month with no payment
gets no invoice. French only.

**Issued once, frozen.** The issuer and buyer are copied onto the row and the
PDF is rendered and stored at issue; nothing re-renders it. Numbers run
without gaps per calendar year: the next number is counted under a lock on the
issuer row, and `Invoice` is append-only, so a number can never be freed.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from billing.models import (
    Invoice,
    InvoiceIssuer,
    InvoiceLine,
    PrepaidKind,
    PrepaidOrder,
    PrepaidState,
)
from billing.rendering import FakePdfRenderer, PdfRenderPort, get_pdf_renderer
from billing.services.prepaid import TUNIS, month_start

logger = logging.getLogger(__name__)


def _label(order: PrepaidOrder) -> str:
    if order.kind == PrepaidKind.PLAN_PERIOD and order.plan is not None:
        label = f"Abonnement {order.plan.display_name}, {order.months} mois"
        if order.workspaces:
            label += f", dont {order.workspaces} espace(s) de travail supplémentaire(s)"
        return label
    if order.kind == PrepaidKind.PACK and order.pack is not None:
        return f"Pack {order.pack.display_name}"
    return (
        f"{order.workspaces} espace(s) de travail supplémentaire(s), jusqu'à la fin de la période"
    )


def _money(currency: Any, minor: int) -> str:
    return str(currency.format(minor))


def issue_due(now: dt.datetime | None = None) -> int:
    """Issues every past month's invoice not yet issued. Returns how many.

    Run daily: on the 1st it issues last month's, and any day after it catches
    up a month a failed run missed. Refuses to issue anything while the issuer
    is incomplete, or on a deployment that would write the fake's HTML.
    """
    now = now or timezone.now()
    issuer = InvoiceIssuer.get()
    if issuer.missing:
        logger.error("invoice issuer incomplete; nothing issued", extra={"missing": issuer.missing})
        return 0
    renderer = get_pdf_renderer()
    if isinstance(renderer, FakePdfRenderer) and getattr(settings, "FAKE_BILLING_REFUSES", False):
        logger.error("no PDF engine on this deployment; no invoice issued")
        return 0

    due = (
        PrepaidOrder.objects.filter(
            state=PrepaidState.PAID,
            paid_at__lt=month_start(now),
            invoice_lines__isnull=True,
        )
        .select_related("organization", "currency", "plan", "pack")
        .order_by("paid_at", "pk")
    )
    groups: dict[tuple[int, dt.date], list[PrepaidOrder]] = defaultdict(list)
    for order in due:
        assert order.paid_at is not None
        groups[(order.organization_id, month_start(order.paid_at).date())].append(order)

    issued = 0
    for (_, period), orders in sorted(groups.items(), key=lambda item: item[0][1]):
        issue(orders, period=period, renderer=renderer)
        issued += 1
    return issued


@transaction.atomic
def issue(orders: list[PrepaidOrder], *, period: dt.date, renderer: PdfRenderPort) -> Invoice:
    """One invoice for `orders`, all one organization's payments in `period`."""
    issuer = InvoiceIssuer.objects.select_for_update().get(pk=InvoiceIssuer.get().pk)
    organization = orders[0].organization
    currency = orders[0].currency
    issued_at = timezone.now()
    year = issued_at.astimezone(TUNIS).year
    year_start = dt.datetime(year, 1, 1, tzinfo=TUNIS)
    sequence = Invoice.objects.filter(issued_at__gte=year_start).count() + 1
    number = issuer.number_format.format(year=year, seq=sequence)

    latest = orders[-1]
    labels = [(order, _label(order)) for order in orders]
    net = sum(order.net_minor for order in orders)
    vat = sum(order.vat_minor for order in orders)
    stamp = sum(order.stamp_minor for order in orders)
    issuer_snapshot = {
        "legal_name": issuer.legal_name,
        "address": issuer.address,
        "tax_id": issuer.tax_id,
        "trade_register": issuer.trade_register,
        "email": issuer.email,
    }
    html = render_to_string(
        "billing/invoice.html",
        {
            "number": number,
            "issued_on": issued_at.astimezone(TUNIS).date(),
            "period": period,
            "issuer": issuer_snapshot,
            "buyer_name": latest.buyer_name or organization.name,
            "buyer_tax_id": latest.buyer_tax_id,
            "lines": [
                {
                    "label": label,
                    "net": _money(currency, order.net_minor),
                    "vat": _money(currency, order.vat_minor),
                    "rate": f"{(order.vat_rate * 100).normalize()} %",
                }
                for order, label in labels
            ],
            "credit_note_of": None,
            "net": _money(currency, net),
            "vat": _money(currency, vat),
            "stamp": _money(currency, stamp),
            "total": _money(currency, net + vat + stamp),
        },
    )
    invoice = Invoice(
        organization=organization,
        period=period,
        number=number,
        currency=currency,
        issuer=issuer_snapshot,
        buyer_name=latest.buyer_name or organization.name,
        buyer_tax_id=latest.buyer_tax_id,
        net_minor=net,
        vat_minor=vat,
        stamp_minor=stamp,
        total_minor=net + vat + stamp,
    )
    invoice.document.save(
        f"{number}.{renderer.extension}", ContentFile(renderer.render(html)), save=False
    )
    invoice.save()
    for order, label in labels:
        InvoiceLine.objects.create(
            invoice=invoice,
            order=order,
            label=label,
            net_minor=order.net_minor,
            vat_minor=order.vat_minor,
        )
    logger.info(
        "invoice issued",
        extra={"invoice": number, "organization_id": organization.pk, "orders": len(orders)},
    )
    return invoice


@transaction.atomic
def issue_credit_note(invoice: Invoice, *, reason: str) -> Invoice:
    """An *avoir* cancelling `invoice` in full: same amounts, negated, its own
    number in the same sequence. A partial correction is a credit note plus a
    new invoice; the original is never edited (append-only)."""
    if invoice.corrects_id is not None or invoice.credit_notes.exists():
        from common.exceptions import StateConflict

        raise StateConflict("This invoice is already corrected.", code="already_corrected")
    issuer = InvoiceIssuer.objects.select_for_update().get(pk=InvoiceIssuer.get().pk)
    renderer = get_pdf_renderer()
    currency = invoice.currency
    issued_at = timezone.now()
    year = issued_at.astimezone(TUNIS).year
    sequence = (
        Invoice.objects.filter(issued_at__gte=dt.datetime(year, 1, 1, tzinfo=TUNIS)).count() + 1
    )
    number = issuer.number_format.format(year=year, seq=sequence)
    html = render_to_string(
        "billing/invoice.html",
        {
            "number": number,
            "issued_on": issued_at.astimezone(TUNIS).date(),
            "period": invoice.period,
            "issuer": invoice.issuer,
            "buyer_name": invoice.buyer_name,
            "buyer_tax_id": invoice.buyer_tax_id,
            "credit_note_of": invoice.number,
            "reason": reason,
            "lines": [],
            "net": _money(currency, -invoice.net_minor),
            "vat": _money(currency, -invoice.vat_minor),
            "stamp": _money(currency, -invoice.stamp_minor),
            "total": _money(currency, -invoice.total_minor),
        },
    )
    note = Invoice(
        organization=invoice.organization,
        period=invoice.period,
        number=number,
        currency=currency,
        issuer=invoice.issuer,
        buyer_name=invoice.buyer_name,
        buyer_tax_id=invoice.buyer_tax_id,
        net_minor=-invoice.net_minor,
        vat_minor=-invoice.vat_minor,
        stamp_minor=-invoice.stamp_minor,
        total_minor=-invoice.total_minor,
        corrects=invoice,
    )
    note.document.save(
        f"{number}.{renderer.extension}", ContentFile(renderer.render(html)), save=False
    )
    note.save()
    logger.info("credit note issued", extra={"invoice": invoice.number, "credit_note": number})
    return note
