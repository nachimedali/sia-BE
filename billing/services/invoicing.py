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
import hashlib
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


def fr_money(currency: Any, minor: int) -> str:
    """`1234500` TND → `1 234,500 DT`: French separators, the currency's own decimals."""
    major, rest = divmod(abs(minor), 10**currency.minor_units)
    amount = f"{major:,}".replace(",", "\u00a0")
    if currency.minor_units:
        amount += f",{rest:0{currency.minor_units}d}"
    symbol = currency.symbol or currency.code
    return f"{'-' if minor < 0 else ''}{amount} {symbol}"


_UNITS = [
    "zéro",
    "un",
    "deux",
    "trois",
    "quatre",
    "cinq",
    "six",
    "sept",
    "huit",
    "neuf",
    "dix",
    "onze",
    "douze",
    "treize",
    "quatorze",
    "quinze",
    "seize",
]
_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
#: Spelled-out names of each currency's units; only TND is invoiced by us.
_UNIT_NAMES = {"TND": ("dinar", "millime")}


def _below_100(n: int) -> str:
    if n < 17:
        return _UNITS[n]
    if n < 20:
        return f"dix-{_UNITS[n - 10]}"
    tens, unit = divmod(n, 10)
    if tens in (7, 9):  # soixante-dix, quatre-vingt-dix: the teens ride on 60 and 80
        head = "soixante" if tens == 7 else "quatre-vingt"
        joiner = " et " if n == 71 else "-"
        return f"{head}{joiner}{_below_100(10 + unit)}"
    head = "quatre-vingt" if tens == 8 else _TENS[tens]
    if unit == 0:
        return "quatre-vingts" if tens == 8 else head
    return f"{head}{' et ' if unit == 1 and tens != 8 else '-'}{_UNITS[unit]}"


def _below_1000(n: int, *, final: bool) -> str:
    """`final`: nothing but a noun follows, so 80 and 200 take their plural s."""
    hundreds, rest = divmod(n, 100)
    words = []
    if hundreds:
        cent = "cent" if hundreds == 1 else f"{_UNITS[hundreds]} cent"
        words.append(cent + ("s" if hundreds > 1 and not rest and final else ""))
    if rest:
        tail = _below_100(rest)
        words.append(tail if final or not tail.endswith("vingts") else tail[:-1])
    return " ".join(words)


def _in_words(n: int) -> str:
    if n == 0:
        return "zéro"
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1000)
    words = []
    if millions:
        words.append(f"{_below_1000(millions, final=True)} million{'s' if millions > 1 else ''}")
    if thousands:
        words.append("mille" if thousands == 1 else f"{_below_1000(thousands, final=False)} mille")
    if units:
        words.append(_below_1000(units, final=True))
    return " ".join(words)


def amount_in_words(currency: Any, minor: int) -> str:
    """`279000` TND → `Deux cent soixante-dix-neuf dinars`, as an invoice is *arrêtée*."""
    major, rest = divmod(abs(minor), 10**currency.minor_units)
    unit, sub = _UNIT_NAMES.get(currency.code, (currency.code, ""))
    words = f"{_in_words(major)} {unit}{'s' if major > 1 and unit != currency.code else ''}"
    if rest and sub:
        words += f" et {_in_words(rest)} {sub}{'s' if rest > 1 else ''}"
    return words[0].upper() + words[1:]


def _pct(rate: Any) -> str:
    return f"{(rate * 100).normalize()} %"


def context(
    *,
    kind: str,
    number: str,
    issued_on: dt.date,
    period: dt.date,
    issuer: dict[str, str],
    buyer_name: str,
    buyer_tax_id: str,
    currency: Any,
    lines: list[tuple[PrepaidOrder, str]],
    corrects: Invoice | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Everything `billing/invoice.html` prints, already formatted in French.

    A credit note lists the corrected invoice's lines and refunds net + VAT:
    the stamp duty is not refunded, so it has no stamp row.
    """

    def money(minor: int) -> str:
        return fr_money(currency, minor)

    net = sum(order.net_minor for order, _ in lines)
    vat = sum(order.vat_minor for order, _ in lines)
    stamp = sum(order.stamp_minor for order, _ in lines) if kind == "invoice" else 0
    by_rate: dict[Any, list[int]] = defaultdict(lambda: [0, 0])
    for order, _ in lines:
        by_rate[order.vat_rate][0] += order.net_minor
        by_rate[order.vat_rate][1] += order.vat_minor
    return {
        "kind": kind,
        "title": "AVOIR" if kind == "credit_note" else "FACTURE",
        "number": number,
        "issue_date": issued_on.strftime("%d/%m/%Y"),
        "month": f"{_MONTHS[period.month - 1]} {period.year}",
        "issuer": {
            "legal_name": issuer.get("legal_name", ""),
            "address": issuer.get("address", ""),
            "address_line": ", ".join(
                part.strip() for part in issuer.get("address", "").splitlines() if part.strip()
            ),
            "matricule_fiscal": issuer.get("tax_id", ""),
            "rc": issuer.get("trade_register", ""),
            "email": issuer.get("email", ""),
        },
        "client": {"name": buyer_name, "tax_id": buyer_tax_id},
        "lines": [
            {
                "designation": label,
                "detail": f"Payé le {order.paid_at.astimezone(TUNIS):%d/%m/%Y}"
                if order.paid_at
                else "",
                "ref": order.gateway_ref or "",
                "ht": money(order.net_minor),
                "rate": _pct(order.vat_rate),
                "tva": money(order.vat_minor),
            }
            for order, label in lines
        ],
        "vat_breakdown": [
            {"rate": _pct(rate), "base": money(base), "tva": money(tax)}
            for rate, (base, tax) in sorted(by_rate.items())
        ],
        "total_ht": money(net),
        "total_tva": money(vat),
        "stamp": money(stamp),
        "total_ttc": money(net + vat + stamp),
        "total_in_words": amount_in_words(currency, net + vat + stamp),
        "paid_with": "Konnect",
        "charged": money(sum(order.amount_minor for order, _ in lines)),
        "corrects": corrects.number if corrects else "",
        "corrects_date": f"{corrects.issued_at.astimezone(TUNIS):%d/%m/%Y}" if corrects else "",
        "reason": reason,
    }


_MONTHS = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]


def _store(invoice: Invoice, html: str, renderer: PdfRenderPort) -> None:
    """Renders once and records the file's hash; nothing renders it again."""
    data = renderer.render(html)
    invoice.sha256 = hashlib.sha256(data).hexdigest()
    invoice.document.save(f"{invoice.number}.{renderer.extension}", ContentFile(data), save=False)


def next_issue_on(now: dt.datetime | None = None) -> dt.date:
    """The 1st of next month (Africa/Tunis): when this month's payments are invoiced."""
    start = month_start(now or timezone.now())
    return (start + dt.timedelta(days=32)).date().replace(day=1)


def late_month(organization: Any, now: dt.datetime | None = None) -> dt.date | None:
    """The earliest past month with a payment still waiting for its invoice, if any.

    Normally none: the job issues on the 1st. It shows when issuing is held
    back (issuer incomplete, no PDF engine) or a run failed.
    """
    paid_at = (
        PrepaidOrder.objects.filter(
            organization=organization,
            state=PrepaidState.PAID,
            paid_at__lt=month_start(now or timezone.now()),
            invoice_lines__isnull=True,
        )
        .order_by("paid_at")
        .values_list("paid_at", flat=True)
        .first()
    )
    return month_start(paid_at).date() if paid_at else None


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
    buyer_name = latest.buyer_name or organization.name
    html = render_to_string(
        "billing/invoice.html",
        context(
            kind="invoice",
            number=number,
            issued_on=issued_at.astimezone(TUNIS).date(),
            period=period,
            issuer=issuer_snapshot,
            buyer_name=buyer_name,
            buyer_tax_id=latest.buyer_tax_id,
            currency=currency,
            lines=labels,
        ),
    )
    invoice = Invoice(
        organization=organization,
        period=period,
        number=number,
        currency=currency,
        issuer=issuer_snapshot,
        buyer_name=buyer_name,
        buyer_tax_id=latest.buyer_tax_id,
        net_minor=net,
        vat_minor=vat,
        stamp_minor=stamp,
        total_minor=net + vat + stamp,
    )
    _store(invoice, html, renderer)
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
    """An *avoir* cancelling `invoice` in full: net and VAT negated, its own
    number in the same sequence. The stamp duty is not refunded. A partial
    correction is a credit note plus a new invoice; the original is never
    edited (append-only)."""
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
    lines = [(line.order, line.label) for line in invoice.lines.select_related("order")]
    html = render_to_string(
        "billing/invoice.html",
        context(
            kind="credit_note",
            number=number,
            issued_on=issued_at.astimezone(TUNIS).date(),
            period=invoice.period,
            issuer=invoice.issuer,
            buyer_name=invoice.buyer_name,
            buyer_tax_id=invoice.buyer_tax_id,
            currency=currency,
            lines=lines,
            corrects=invoice,
            reason=reason,
        ),
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
        stamp_minor=0,
        total_minor=-(invoice.net_minor + invoice.vat_minor),
        corrects=invoice,
        reason=reason[:280],
    )
    _store(note, html, renderer)
    note.save()
    logger.info("credit note issued", extra={"invoice": invoice.number, "credit_note": number})
    return note
