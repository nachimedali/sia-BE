"""Notification fan-out (P2-12).

Two properties matter more than the endpoints:

* **Nothing here runs on a signal** (Part 7 rule 8) — asserted directly, because
  a signal is exactly the shortcut this design forbids and nothing else would
  catch it.
* **A transport failing does not lose the others** — the in-app row is the
  record that somebody was told, and an SMTP outage must not take it with it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import patch

import pytest
from django.utils import timezone

from collaboration.services import add_comment, assign, open_thread
from content.services.posts import create_post
from notifications.models import (
    DEFAULT_TRANSPORTS,
    EventKey,
    Notification,
    NotificationPreference,
    Transport,
)
from notifications.services import mark_read, notify, transports_for
from workspaces.services import approvals

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/notifications/"
PREFS_URL = "/api/v1/notifications/preferences/"


@pytest.fixture
def draft(advanced_workspace: Any, contributor_user: Any) -> Any:
    return create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Autumn launch"
    )


def _for(user: Any) -> Any:
    return Notification.objects.filter(user=user)


# -----------------------------------------------------------------------------
# Part 7 rule 8 — explicit calls, no signals
# -----------------------------------------------------------------------------
def test_the_notifications_app_connects_no_signals() -> None:
    """A signal makes "why did I get this email" a search across the codebase,
    and makes fan-out fire from a fixture load nobody meant to notify anyone
    about."""
    import inspect

    from django.db.models import signals

    import collaboration.services
    import notifications.services
    import notifications.tasks
    import workspaces.services.approvals

    for module in (
        notifications.services,
        notifications.tasks,
        collaboration.services,
        workspaces.services.approvals,
    ):
        source = inspect.getsource(module)
        assert "signals" not in source
        assert "receiver" not in source

    # And nothing has quietly registered one at import time either.
    for signal in (signals.post_save, signals.post_delete, signals.m2m_changed):
        registered = {getattr(receiver[1], "__module__", "") for receiver in signal.receivers}
        assert not any(name.startswith("notifications") for name in registered)


# -----------------------------------------------------------------------------
# Event sites
# -----------------------------------------------------------------------------
def test_submitting_tells_the_people_who_can_approve(
    draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    approvals.submit_for_review(draft, actor=contributor_user)

    assert _for(admin_user).get().event_key == EventKey.POST_SUBMITTED
    # Not the person who did it: being told about your own action is noise that
    # trains people to ignore the channel.
    assert not _for(contributor_user).exists()


def test_a_named_stage_narrows_who_is_told(
    advanced_workspace: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    """A stage that names its approvers is a statement about people. Telling
    everyone with `approve` anyway would make naming them pointless."""
    from django.contrib.auth import get_user_model

    from workspaces.models import ApprovalStage, Membership, Role

    other = get_user_model().objects.create_user(email="legal@example.com", password="x")
    Membership.objects.create(user=other, workspace=advanced_workspace, role=Role.ADMIN)
    stage = ApprovalStage.objects.create(
        chain=approvals.default_chain(advanced_workspace), order=1, name="Legal"
    )
    stage.required_approvers.set([other])

    approvals.submit_for_review(draft, actor=contributor_user)

    assert _for(other).exists()
    assert not _for(admin_user).exists()


def test_an_unnamed_stage_falls_back_to_everyone_who_can_approve(
    advanced_workspace: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    """A single-stage chain names nobody on purpose, and an approval request
    that reaches no inbox is a post that never goes out."""
    from workspaces.models import ApprovalStage

    ApprovalStage.objects.create(
        chain=approvals.default_chain(advanced_workspace), order=1, name="Review"
    )

    approvals.submit_for_review(draft, actor=contributor_user)

    assert _for(admin_user).exists()


def test_approving_tells_the_author(draft: Any, contributor_user: Any, admin_user: Any) -> None:
    post = approvals.submit_for_review(draft, actor=contributor_user)
    approvals.approve(post, actor=admin_user)

    assert (
        _for(contributor_user).values_list("event_key", flat=True).first() == EventKey.POST_APPROVED
    )


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("request_changes", EventKey.POST_CHANGES_REQUESTED),
        ("reject", EventKey.POST_REJECTED),
    ],
)
def test_a_refusal_tells_the_author_which_kind_it_was(
    draft: Any, contributor_user: Any, admin_user: Any, action: str, expected: str
) -> None:
    """Two refusals with different consequences — one asks for an edit, the
    other ends it — so they must not arrive as the same message."""
    post = approvals.submit_for_review(draft, actor=contributor_user)
    getattr(approvals, action)(post, actor=admin_user, note="Not this one")

    assert _for(contributor_user).first().event_key == expected


def test_a_reply_reaches_everyone_already_in_the_conversation(
    draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    """A reply that reaches only the thread's owner leaves the person who
    raised the point in the dark."""
    thread = open_thread(draft, author=contributor_user, title="Hook", body="Too flat")

    add_comment(thread, author=admin_user, body="Agreed — try a question")

    assert _for(contributor_user).filter(event_key=EventKey.THREAD_COMMENTED).exists()
    assert not _for(admin_user).filter(event_key=EventKey.THREAD_COMMENTED).exists()


def test_being_both_author_and_assignee_is_told_once(
    draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    thread = open_thread(draft, author=contributor_user, title="Hook", body="Too flat")
    assign(thread, assignee=contributor_user, actor=admin_user)
    Notification.objects.all().delete()

    add_comment(thread, author=admin_user, body="Any thoughts?")

    assert _for(contributor_user).count() == 1


def test_assignment_tells_the_assignee(draft: Any, contributor_user: Any, admin_user: Any) -> None:
    thread = open_thread(draft, author=contributor_user, title="Hook", body="Too flat")

    assign(thread, assignee=admin_user, actor=contributor_user)

    assert _for(admin_user).filter(event_key=EventKey.THREAD_ASSIGNED).exists()


# -----------------------------------------------------------------------------
# Preferences
# -----------------------------------------------------------------------------
def test_a_missing_preference_means_the_default_not_silence(
    advanced_workspace: Any, admin_user: Any
) -> None:
    """Treating absence as "no transports" would silently mute everyone who has
    not visited the settings screen — which is almost everyone."""
    assert NotificationPreference.objects.count() == 0

    assert transports_for(admin_user, advanced_workspace, EventKey.POST_SUBMITTED) == list(
        DEFAULT_TRANSPORTS[EventKey.POST_SUBMITTED]
    )


def test_thread_replies_do_not_email_by_default() -> None:
    """A busy thread emails a person into muting the product, and the one
    preference everybody eventually changes should be the default."""
    assert Transport.EMAIL not in DEFAULT_TRANSPORTS[EventKey.THREAD_COMMENTED]
    assert Transport.EMAIL in DEFAULT_TRANSPORTS[EventKey.POST_SUBMITTED]


def test_a_preference_of_no_transports_is_respected(
    draft: Any, advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """An explicitly empty list is a choice, not a missing row."""
    NotificationPreference.objects.create(
        user=admin_user,
        workspace=advanced_workspace,
        event_key=EventKey.POST_SUBMITTED,
        transports=[],
    )

    approvals.submit_for_review(draft, actor=contributor_user)

    assert not _for(admin_user).exists()


def test_preferences_are_per_workspace(
    advanced_workspace: Any, admin_user: Any, other_user: Any
) -> None:
    """Someone who runs their own brand and also reviews for an agency wants
    different noise from each."""
    from workspaces.services.provisioning import provision_workspace

    theirs = provision_workspace(other_user, name="Another Company")
    NotificationPreference.objects.create(
        user=admin_user,
        workspace=advanced_workspace,
        event_key=EventKey.POST_SUBMITTED,
        transports=[],
    )

    assert transports_for(admin_user, advanced_workspace, EventKey.POST_SUBMITTED) == []
    assert transports_for(admin_user, theirs, EventKey.POST_SUBMITTED) != []


def test_the_preferences_endpoint_returns_a_row_for_every_event(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    """A settings screen draws one row per event; a client filling the gaps
    itself would be a second copy of the defaults to drift from."""
    rows = client_as(admin_user).get(PREFS_URL).json()

    assert {row["event_key"] for row in rows} == set(DEFAULT_TRANSPORTS)
    assert all(row["is_default"] for row in rows)


def test_setting_a_preference_persists_and_stops_defaulting(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    api = client_as(admin_user)

    rows = api.put(
        PREFS_URL,
        {"event_key": EventKey.POST_SUBMITTED, "transports": [Transport.IN_APP]},
        format="json",
    ).json()

    row = next(r for r in rows if r["event_key"] == EventKey.POST_SUBMITTED)
    assert row["transports"] == [Transport.IN_APP]
    assert row["is_default"] is False


def test_an_unknown_transport_is_a_400(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    response = client_as(admin_user).put(
        PREFS_URL,
        {"event_key": EventKey.POST_SUBMITTED, "transports": ["carrier_pigeon"]},
        format="json",
    )

    assert response.status_code == 400


# -----------------------------------------------------------------------------
# Delivery
# -----------------------------------------------------------------------------
def test_one_failing_transport_does_not_lose_the_others(
    advanced_workspace: Any, admin_user: Any, outbox: Any
) -> None:
    """An SMTP outage must not take the in-app record with it — that record is
    the evidence the person was told, and the cheapest of the three to write."""
    with patch(
        "notifications.transports.EmailTransport.deliver", side_effect=RuntimeError("smtp down")
    ):
        notify(
            users=[admin_user],
            workspace=advanced_workspace,
            event_key=EventKey.POST_SUBMITTED,
            payload={"subject": "Something happened"},
        )

    assert _for(admin_user).count() == 1
    assert outbox == []


def test_push_ships_as_a_declared_seam_with_a_fake_behind_it(
    advanced_workspace: Any, admin_user: Any, push_transport: Any
) -> None:
    """P2-12/Phase 10. A `push` preference that silently did nothing would be
    worse than no push at all, so the fake keeps a record."""
    NotificationPreference.objects.create(
        user=admin_user,
        workspace=advanced_workspace,
        event_key=EventKey.POST_SUBMITTED,
        transports=[Transport.PUSH],
    )

    notify(
        users=[admin_user],
        workspace=advanced_workspace,
        event_key=EventKey.POST_SUBMITTED,
        payload={"subject": "Something happened"},
    )

    assert [sent["user"] for sent in push_transport.sent] == [admin_user.pk]


def test_a_recipient_deleted_before_delivery_is_not_an_error() -> None:
    from notifications.tasks import deliver_notification

    assert (
        deliver_notification(
            user_id=999_999,
            workspace_id=999_999,
            event_key=EventKey.POST_SUBMITTED,
            payload={},
            transports=[Transport.IN_APP],
        )
        == 0
    )


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------
def test_the_list_is_scoped_to_the_person_not_only_the_workspace(
    client_as: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    """A colleague reading your notifications would be a leak inside a tenant
    rather than across one."""
    approvals.submit_for_review(draft, actor=contributor_user)

    mine = client_as(admin_user).get(LIST_URL).json()["results"]
    theirs = client_as(contributor_user).get(LIST_URL).json()["results"]

    assert len(mine) == 1
    assert theirs == []


def test_marking_read_writes_only_read_at(
    draft: Any, contributor_user: Any, admin_user: Any, advanced_workspace: Any
) -> None:
    """The one mutation any service performs. A notification says what was true
    when it was sent."""
    approvals.submit_for_review(draft, actor=contributor_user)
    before = _for(admin_user).get()

    mark_read(user=admin_user, workspace=advanced_workspace)

    after = _for(admin_user).get()
    assert after.read_at is not None
    assert after.event_key == before.event_key
    assert after.payload == before.payload
    assert after.created_at == before.created_at


def test_marking_read_is_idempotent_and_reports_what_it_changed(
    client_as: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    approvals.submit_for_review(draft, actor=contributor_user)
    api = client_as(admin_user)

    assert api.post(LIST_URL, {}, format="json").json()["marked_read"] == 1
    assert api.post(LIST_URL, {}, format="json").json()["marked_read"] == 0


def test_unread_filters_the_bell(
    client_as: Any, draft: Any, contributor_user: Any, admin_user: Any, advanced_workspace: Any
) -> None:
    approvals.submit_for_review(draft, actor=contributor_user)
    api = client_as(admin_user)
    assert len(api.get(f"{LIST_URL}?unread=true").json()["results"]) == 1

    mark_read(user=admin_user, workspace=advanced_workspace)

    assert api.get(f"{LIST_URL}?unread=true").json()["results"] == []
    assert len(api.get(LIST_URL).json()["results"]) == 1


def test_marking_specific_ids_leaves_the_rest(
    draft: Any, contributor_user: Any, admin_user: Any, advanced_workspace: Any
) -> None:
    approvals.submit_for_review(draft, actor=contributor_user)
    Notification.objects.create(
        user=admin_user,
        workspace=advanced_workspace,
        event_key=EventKey.THREAD_COMMENTED,
        payload={},
        created_at=timezone.now() - dt.timedelta(minutes=1),
    )
    first = _for(admin_user).first()

    assert mark_read(user=admin_user, workspace=advanced_workspace, ids=[first.pk]) == 1

    assert _for(admin_user).filter(read_at__isnull=True).count() == 1
