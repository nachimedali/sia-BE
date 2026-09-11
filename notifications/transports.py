"""Notification transports — the port and its implementations (P2-12).

**A Protocol with a fake, like every other external dependency** (Part 7 rule
6). A fresh checkout delivers in-app notifications and records emails in the
fake outbox, with no third-party account anywhere.

Adding Slack in Phase 10 is one class and one enum member. That is the whole
reason this abstraction exists now rather than then: the alternative is finding
every `send_mail` call in a codebase that has grown four more event sites.
"""

from __future__ import annotations

from typing import Any, Protocol

from notifications.models import Notification, Transport


class NotificationTransport(Protocol):
    key: str

    def deliver(
        self, *, user: Any, workspace: Any, event_key: str, payload: dict[str, Any]
    ) -> None: ...


class InAppTransport:
    """Writes the row the bell reads. **Always available and never optional in
    practice** — it is also the record that the person was told, which is what
    a later "nobody told me" turns on."""

    key: str = Transport.IN_APP

    def deliver(
        self, *, user: Any, workspace: Any, event_key: str, payload: dict[str, Any]
    ) -> None:
        Notification.objects.create(
            user=user, workspace=workspace, event_key=event_key, payload=payload
        )


class EmailTransport:
    """Goes through the existing `MailSender` port, so the fake outbox every
    test already uses covers this too."""

    key: str = Transport.EMAIL

    def deliver(
        self, *, user: Any, workspace: Any, event_key: str, payload: dict[str, Any]
    ) -> None:
        from common.mail import Email, get_mail_sender

        get_mail_sender().send(
            Email(
                to=user.email,
                subject=payload.get("subject", "An update on your content"),
                template="notification",
                context={"workspace": workspace, "event_key": event_key, **payload},
            )
        )


class FakePushTransport:
    """**Declared, with no provider behind it** (P2-12, Phase 10).

    Records instead of sending, exactly as `FakeMailSender` does. Shipping the
    seam now costs one class; a `push` preference that silently does nothing
    would be worse than no push at all, so `deliver` keeps a record a test can
    assert on rather than passing quietly.
    """

    key: str = Transport.PUSH

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def deliver(
        self, *, user: Any, workspace: Any, event_key: str, payload: dict[str, Any]
    ) -> None:
        self.sent.append(
            {"user": user.pk, "workspace": workspace.pk, "event_key": event_key, **payload}
        )

    def clear(self) -> None:
        self.sent.clear()


#: Module-level so a test can inspect what a view sent after it has run — the
#: same shape `common.mail._fake_sender` and the fake publish adapter use, and
#: the same reason `conftest.py` resets it between tests.
_fake_push = FakePushTransport()


def registry() -> dict[str, NotificationTransport]:
    """Every transport this deployment can deliver through.

    A function rather than a module constant so that swapping the push
    implementation for a real one is a settings change, not an import-order
    puzzle.
    """
    return {
        Transport.IN_APP: InAppTransport(),
        Transport.EMAIL: EmailTransport(),
        Transport.PUSH: _fake_push,
    }
