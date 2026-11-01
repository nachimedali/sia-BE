"""HTML to PDF for invoices (TN-12), a port with a fake like every engine here.

WeasyPrint is the real one (owner, 2026-11-01): Python, no browser, needs Pango
on the server. The fake returns the HTML itself, labelled as HTML, for the
reason `analytics.providers.rendering` gives — a fake that emitted a PDF header
would let a broken pipeline look like a working one.
"""

from __future__ import annotations

from typing import Protocol

from django.conf import settings


class PdfRenderPort(Protocol):
    content_type: str
    extension: str

    def render(self, html: str) -> bytes: ...


class FakePdfRenderer:
    content_type = "text/html"
    extension = "html"

    def render(self, html: str) -> bytes:
        return html.encode()


class WeasyPrintRenderer:
    content_type = "application/pdf"
    extension = "pdf"

    def render(self, html: str) -> bytes:
        from weasyprint import HTML  # type: ignore[import-untyped]

        pdf: bytes = HTML(string=html).write_pdf()
        return pdf


def get_pdf_renderer() -> PdfRenderPort:
    if getattr(settings, "USE_FAKE_PDF_RENDERER", True):
        return FakePdfRenderer()
    return WeasyPrintRenderer()
