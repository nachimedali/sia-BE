"""Notification fan-out (P2-12).

One entry point — `notify` — called **explicitly** from each event site. Not a
signal (Part 7 rule 8): a signal makes "why did I get this email" a search
across the whole codebase, and makes fan-out fire from a migration or a fixture
load nobody meant to notify anyone about.

Delivery is queued on `notify_q` rather than run in the request. Someone is
waiting on the request, nobody is waiting on the email, and a slow SMTP server
must not be able to make approving a post feel broken.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.utils import timezone

from notifications.models import (
    DEFAULT_TRANSPORTS,
    EventKey,
    Notification,
    NotificationPreference,
)


def transports_for(user: Any, workspace: Any, event_key: str) -> list[str]:
    """This person's chosen transports for this event, or the default.

    A missing row means the default, never "none": treating absence as "no
    transports" would silently mute everyone who has not visited the settings
    screen, which is almost everyone.
    """
    preference = NotificationPreference.objects.filter(
        user=user, workspace=workspace, event_key=event_key
    ).first()
    if preference is None:
        return list(DEFAULT_TRANSPORTS.get(event_key, []))
    return list(preference.transports)


def notify(
    *,
    users: Iterable[Any],
    workspace: Any,
    event_key: str,
    payload: dict[str, Any],
    exclude: Any = None,
) -> int:
    """Fan out one event. Returns how many people were queued for.

    `exclude` drops the actor. Being told about your own action is noise that
    trains people to ignore the channel, and every caller would otherwise have
    to remember to filter — so the filter lives here, once.

    Deduplicated by id: a person who is both the author and the assignee of a
    thread is one person and should be told once.
    """
    recipients = {
        user.pk: user
        for user in users
        if user is not None and (exclude is None or user.pk != exclude.pk)
    }
    for user in recipients.values():
        chosen = transports_for(user, workspace, event_key)
        if not chosen:
            continue
        _queue(
            user_id=user.pk,
            workspace_id=workspace.pk,
            event_key=event_key,
            payload=payload,
            transports=chosen,
        )
    return len(recipients)


def _queue(**kwargs: Any) -> None:
    """Imported at call time: `notifications.tasks` imports this module for the
    delivery body, so a module-level import would be a cycle."""
    from notifications.tasks import deliver_notification

    deliver_notification.delay(**kwargs)


def mark_read(*, user: Any, workspace: Any, ids: list[int] | None = None) -> int:
    """Mark notifications read. With no ids, marks everything unread in this
    workspace — the "clear all" the bell needs.

    A bulk `update`, and it writes **only** `read_at`. Nothing in this module
    ever rewrites `event_key` or `payload`; a notification says what was true
    when it was sent.
    """
    queryset = Notification.objects.filter(user=user, workspace=workspace, read_at__isnull=True)
    if ids is not None:
        queryset = queryset.filter(pk__in=ids)
    return int(queryset.update(read_at=timezone.now()))


# -----------------------------------------------------------------------------
# Event sites — one function per event, so a caller names the thing that
# happened rather than assembling a payload of its own. Two call sites building
# the same payload differently is how one of them ends up saying "None".
# -----------------------------------------------------------------------------
def post_event(post: Any, *, event_key: str, actor: Any, recipients: Iterable[Any]) -> int:
    return notify(
        users=recipients,
        workspace=post.workspace,
        event_key=event_key,
        payload={
            "post": post.pk,
            "title": str(post),
            "actor": getattr(actor, "email", "") if actor is not None else "",
            "subject": f"{EventKey(event_key).label}: {post}",
        },
        exclude=actor,
    )


def thread_event(thread: Any, *, event_key: str, actor: Any, recipients: Iterable[Any]) -> int:
    return notify(
        users=recipients,
        workspace=thread.workspace,
        event_key=event_key,
        payload={
            "thread": thread.pk,
            "post": thread.post_id,
            "title": thread.title,
            "actor": getattr(actor, "email", "") if actor is not None else "",
            "subject": f"{EventKey(event_key).label}: {thread.title}",
        },
        exclude=actor,
    )
