"""The report render port and its fake (P6-05).

**A port with a fake, like every external dependency here** (Part 7 rule 6): a
fresh checkout renders a report end to end with no PDF engine installed and no
third-party account. The real implementation needs a headless browser or a
typesetting library — a system dependency, and the same reason `VideoEditPort`
resolves to `None` rather than pretending.

The port takes **already-rendered sections**, never a queryset. Rendering is
`services.reporting`'s job and happens once, so the numbers on screen and the
numbers in the PDF are the same values rather than two code paths that agree
today.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from django.conf import settings


class ReportRenderPort(Protocol):
    """Turns a rendered report payload into a document."""

    #: What the bytes are, so the caller names the file correctly rather than
    #: assuming a PDF from a renderer that may not produce one.
    content_type: str
    extension: str

    def render(self, *, title: str, payload: dict[str, Any]) -> bytes:
        """The document. Raises nothing a caller is expected to recover from —
        a failed render is recorded on the `ReportRun`, not retried blindly."""
        ...


class FakeReportRenderer:
    """Deterministic JSON, not a fake PDF.

    A stub that emitted a PDF header would let a broken pipeline look like a
    working one — the file would open, be blank, and nobody would know which
    half failed. JSON is obviously not a PDF, which is exactly what a fake
    should be: usable end to end and impossible to mistake for the real thing.
    """

    content_type = "application/json"
    extension = "json"

    def __init__(self) -> None:
        self.rendered: list[dict[str, Any]] = []

    def render(self, *, title: str, payload: dict[str, Any]) -> bytes:
        self.rendered.append({"title": title, "payload": payload})
        return json.dumps(
            {"title": title, "payload": payload}, sort_keys=True, default=str
        ).encode()

    def clear(self) -> None:
        self.rendered.clear()


_fake_renderer = FakeReportRenderer()


def get_report_renderer() -> ReportRenderPort:
    """The configured renderer.

    Defaults to the fake whenever no engine is configured, the same
    `USE_FAKE_*` shape billing and storage already use — a fresh checkout runs
    every report path without installing anything.
    """
    if getattr(settings, "USE_FAKE_REPORT_RENDERER", True):
        return _fake_renderer

    raise NotImplementedError(  # pragma: no cover — no engine ships yet
        "No report renderer is configured. Set USE_FAKE_REPORT_RENDERER=false "
        "only once a real engine is wired up."
    )
