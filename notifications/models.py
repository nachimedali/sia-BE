"""Notification fan-out (P2-12).

**Pluggable transports from day one.** Slack (Phase 10) and mobile push
(Phase 10) are the two that are certain to arrive, and retrofitting a transport
abstraction onto a codebase that hardcoded `send_mail` costs far more than
declaring one now — the same argument that put API scopes in Phase 0.

**Nothing here runs on a signal** (Part 7 rule 8). Fan-out is an explicit
service call at each event site, which is what keeps "why did I get this email"
answerable by reading the service that sent it.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models


class EventKey(models.TextChoices):
    """What happened. A closed set, because a preference row keyed on a free
    string is a preference nobody can render a settings screen from."""

    POST_SUBMITTED = "post.submitted", "A post was submitted for review"
    POST_APPROVED = "post.approved", "A post was approved"
    POST_CHANGES_REQUESTED = "post.changes_requested", "Changes were requested"
    POST_REJECTED = "post.rejected", "A post was rejected"
    THREAD_ASSIGNED = "thread.assigned", "A thread was assigned to you"
    THREAD_COMMENTED = "thread.commented", "Someone replied on a thread"


class Transport(models.TextChoices):
    """How it reaches them. `PUSH` ships behind a port with a fake and no
    provider — declared so Phase 10 is a wiring job rather than a redesign, and
    `SLACK` joins the enum there."""

    IN_APP = "in_app", "In the app"
    EMAIL = "email", "Email"
    PUSH = "push", "Push notification"


#: What a member gets when they have never opened the settings screen. In-app
#: for everything (it costs nothing and is the record), email only for the
#: events that need someone to act — an approval request nobody sees is a post
#: that never goes out.
DEFAULT_TRANSPORTS: dict[str, list[str]] = {
    EventKey.POST_SUBMITTED: [Transport.IN_APP, Transport.EMAIL],
    EventKey.POST_APPROVED: [Transport.IN_APP, Transport.EMAIL],
    EventKey.POST_CHANGES_REQUESTED: [Transport.IN_APP, Transport.EMAIL],
    EventKey.POST_REJECTED: [Transport.IN_APP, Transport.EMAIL],
    EventKey.THREAD_ASSIGNED: [Transport.IN_APP, Transport.EMAIL],
    # Deliberately in-app only. A busy thread emails a person into muting the
    # product, and the one preference everybody eventually changes should be
    # the default.
    EventKey.THREAD_COMMENTED: [Transport.IN_APP],
}


class NotificationPreference(models.Model):
    """One person's choice for one event in one workspace.

    Per workspace, not per account: someone who runs their own brand and also
    reviews for an agency wants different noise from each.

    **A missing row means the default**, not "no transports". Storing every
    default for every member of every workspace would be rows that exist only
    to say nothing.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="notification_preferences"
    )
    event_key = models.CharField(max_length=40, choices=EventKey.choices)
    transports = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["event_key"]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "workspace", "event_key"],
                name="unique_notification_preference",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}/{self.workspace_id} {self.event_key}"


class Notification(models.Model):
    """One thing that happened, addressed to one person.

    **Not `AppendOnly`, despite BUILD-PLAN's sketch**, and the reason is in the
    model itself: `read_at` is a later write to the same row. A table that is
    append-only except for one column is not append-only — it is an ordinary
    table with a name that stops anyone reading it carefully. What append-only
    protects is evidence in a dispute (`CreditLedger`, `ApprovalAction`); a
    read receipt is interface state.

    What *is* guaranteed is that nothing rewrites `event_key` or `payload`:
    `mark_read` is the only mutation any service performs, and the test
    `test_only_read_at_is_ever_written` is what keeps that true.

    `payload` is a small denormalised snapshot rather than a set of FKs. A
    notification says what was true when it was sent — "Sam requested changes on
    *Autumn launch*" — and resolving the title live would rewrite history every
    time somebody renamed a post.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="notifications"
    )
    event_key = models.CharField(max_length=40, choices=EventKey.choices)
    payload = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            # The bell asks "what is unread for me here", which no
            # event-leading index helps.
            models.Index(fields=["user", "workspace", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_key} → {self.user_id}"
