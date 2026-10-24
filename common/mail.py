"""MailSender port (design.md §9).

Like every external dependency, mail sits behind a port with a real
implementation and a deterministic fake. The fake is what tests use by default
(design.md A8), so a suite never depends on an SMTP server being reachable.

No mail is ever sent inline in a request/response cycle (implementation.md §4.3)
— callers queue `accounts.tasks.send_templated_email`, which calls through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.mime.image import MIMEImage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.template.loader import get_template, render_to_string

#: The logo travels inside every HTML email as an inline part, referenced by
#: `cid:` — a URL would need a public `SITE_URL` (a dev or misconfigured one
#: showed a broken image) and is blocked by clients that hide remote images.
LOGO_CID = "composevision-logo"
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "email-logo.png"


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    template: str
    context: dict[str, Any] = field(default_factory=dict)


class MailSender(Protocol):
    def send(self, email: Email) -> None: ...


def first_name_of(user: Any) -> str:
    """How an email greets someone. The API has no display name yet, so an
    empty `first_name` falls back to the address's local part — "ines.b"
    reads as "Ines" — rather than "Hello ,"."""
    if getattr(user, "first_name", ""):
        return str(user.first_name)
    local = str(getattr(user, "email", "")).split("@")[0]
    first = local.replace("_", ".").replace("-", ".").split(".")[0]
    return first.capitalize() or "there"


def base_context() -> dict[str, str]:
    """Links every designed email's header and footer carry.

    All of them resolve against `SITE_URL`, so a wrong value shows up as a
    wrong link in every email rather than in one. The logo is not a link: it is
    the inline PNG part `LOGO_CID` (most mail clients will not render SVG).
    """
    site = getattr(settings, "SITE_URL", "").rstrip("/")
    support = parseaddr(getattr(settings, "DEFAULT_FROM_EMAIL", ""))[1]
    return {
        "site_url": site,
        "logo_url": f"cid:{LOGO_CID}",
        "app_url": f"{site}/app",
        "help_url": f"{site}/",
        "about_url": f"{site}/",
        "security_url": f"{site}/privacy",
        "preferences_url": f"{site}/app/settings",
        "calendar_url": f"{site}/app/calendar",
        "report_url": f"mailto:{support}" if support else f"{site}/",
    }


def render(email: Email) -> tuple[str, str]:
    """`<template>.txt`, and `<template>.html` when a design exists — otherwise
    the text inside the shared branded frame (`email/_layout.html`), so every
    email carries the logo.

    Both senders call this, so a template name that does not resolve fails the
    fake exactly as it fails a real send — not only in production.
    """
    context = {**base_context(), "subject": email.subject, **email.context}
    text = render_to_string(f"email/{email.template}.txt", context)
    try:
        html = get_template(f"email/{email.template}.html")
    except TemplateDoesNotExist:
        return text, render_to_string("email/_layout.html", {**context, "body": text})
    return text, html.render(context)


def _logo_part() -> MIMEImage:
    part = MIMEImage(LOGO_PATH.read_bytes(), "png")
    part.add_header("Content-ID", f"<{LOGO_CID}>")
    part.add_header("Content-Disposition", "inline", filename="composevision.png")
    return part


class _RelatedEmail(EmailMultiAlternatives):
    """multipart/related: the logo part belongs to the HTML, not a download."""

    mixed_subtype = "related"


class DjangoMailSender:
    """Sends the rendered email via the configured Django backend.

    The text part is always sent: it is what a client that blocks HTML shows,
    and what the e2e mailbox reads links from. The HTML part always follows,
    with the logo attached inline.
    """

    def send(self, email: Email) -> None:
        text, html = render(email)
        message = _RelatedEmail(
            subject=email.subject,
            body=text,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            to=[email.to],
        )
        message.attach_alternative(html, "text/html")
        message.attach(_logo_part())
        message.send(fail_silently=False)


class FakeMailSender:
    """Records instead of sending. Used by tests and available in dev.

    It still renders, so a missing template or a template error surfaces in
    every test that sends, not only in the ones that use the real backend.
    """

    def __init__(self) -> None:
        self.outbox: list[Email] = []

    def send(self, email: Email) -> None:
        render(email)
        self.outbox.append(email)

    def clear(self) -> None:
        self.outbox.clear()


def get_mail_sender() -> MailSender:
    """Resolves the configured sender. Swapping providers is a settings change."""
    if getattr(settings, "USE_FAKE_MAIL", False):
        return _fake_sender
    return DjangoMailSender()


# Module-level so tests can inspect the outbox after a task has run.
_fake_sender = FakeMailSender()
