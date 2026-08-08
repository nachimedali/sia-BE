"""MailSender port (design.md §9).

Like every external dependency, mail sits behind a port with a real
implementation and a deterministic fake. The fake is what tests use by default
(design.md A8), so a suite never depends on an SMTP server being reachable.

No mail is ever sent inline in a request/response cycle (implementation.md §4.3)
— callers queue `accounts.tasks.send_templated_email`, which calls through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    template: str
    context: dict[str, Any] = field(default_factory=dict)


class MailSender(Protocol):
    def send(self, email: Email) -> None: ...


class DjangoMailSender:
    """Renders `<template>.txt` and sends via the configured Django backend."""

    def send(self, email: Email) -> None:
        context = {**email.context, "site_url": getattr(settings, "SITE_URL", "")}
        body = render_to_string(f"email/{email.template}.txt", context)

        message = EmailMultiAlternatives(
            subject=email.subject,
            body=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@localhost"),
            to=[email.to],
        )
        message.send(fail_silently=False)


class FakeMailSender:
    """Records instead of sending. Used by tests and available in dev."""

    def __init__(self) -> None:
        self.outbox: list[Email] = []

    def send(self, email: Email) -> None:
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
