"""MailSender port (design.md §9).

Like every external dependency, mail sits behind a port with a real
implementation and a deterministic fake. The fake is what tests use by default
(design.md A8), so a suite never depends on an SMTP server being reachable.

No mail is ever sent inline in a request/response cycle (implementation.md §4.3)
— callers queue `accounts.tasks.send_templated_email`, which calls through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Protocol

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.template.loader import get_template, render_to_string


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
    wrong link in every email rather than in one. The logo is a PNG because
    most mail clients will not render SVG.
    """
    site = getattr(settings, "SITE_URL", "").rstrip("/")
    support = parseaddr(getattr(settings, "DEFAULT_FROM_EMAIL", ""))[1]
    return {
        "site_url": site,
        "logo_url": f"{site}/icon-192.png",
        "app_url": f"{site}/app",
        "help_url": f"{site}/",
        "about_url": f"{site}/",
        "security_url": f"{site}/privacy",
        "preferences_url": f"{site}/app/settings",
        "calendar_url": f"{site}/app/calendar",
        "report_url": f"mailto:{support}" if support else f"{site}/",
    }


def render(email: Email) -> tuple[str, str | None]:
    """`<template>.txt`, plus `<template>.html` when a design exists.

    Both senders call this, so a template name that does not resolve fails the
    fake exactly as it fails a real send — not only in production.
    """
    context = {**base_context(), **email.context}
    text = render_to_string(f"email/{email.template}.txt", context)
    try:
        html = get_template(f"email/{email.template}.html")
    except TemplateDoesNotExist:
        return text, None
    return text, html.render(context)


class DjangoMailSender:
    """Sends the rendered email via the configured Django backend.

    The text part is always sent: it is what a client that blocks HTML shows,
    and what the e2e mailbox reads links from.
    """

    def send(self, email: Email) -> None:
        text, html = render(email)
        message = EmailMultiAlternatives(
            subject=email.subject,
            body=text,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            to=[email.to],
        )
        if html is not None:
            message.attach_alternative(html, "text/html")
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
