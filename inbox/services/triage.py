"""Assign, close and annotate an audience comment (S2-03)."""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

from analytics.models import AudienceComment
from inbox.models import CommentNote, CommentTriage, TriageStatus
from workspaces.models import Membership, Workspace

_UNSET: Any = object()


def _assignable(workspace: Workspace, user_id: int) -> Any:
    """A teammate of *this* workspace, else a 400. The id of a user in another
    workspace is indistinguishable from one that does not exist — the error
    never confirms that account is real."""
    membership = (
        Membership.objects.filter(workspace=workspace, user_id=user_id)
        .select_related("user")
        .first()
    )
    if membership is None:
        raise ValidationError({"assignee": "That person is not on this workspace."})
    return membership.user


def update(
    comment: AudienceComment,
    *,
    workspace: Workspace,
    actor: Any,
    status: str | None = None,
    assignee: Any = _UNSET,
) -> CommentTriage:
    """Change the status and/or the assignee. `assignee=None` unassigns; leaving
    it out leaves it as it was."""
    triage, _ = CommentTriage.objects.get_or_create(comment=comment)
    fields = ["updated_by", "updated_at"]
    if status is not None:
        if status not in TriageStatus.values:
            raise ValidationError({"status": "Use open or done."})
        triage.status = status
        fields.append("status")
    if assignee is not _UNSET:
        triage.assignee = None if assignee is None else _assignable(workspace, int(assignee))
        fields.append("assignee")
    triage.updated_by = actor
    triage.save(update_fields=fields)
    return triage


def add_note(comment: AudienceComment, *, actor: Any, body: str) -> CommentNote:
    text = body.strip()
    if not text:
        raise ValidationError({"body": "A note cannot be empty."})
    return CommentNote.objects.create(comment=comment, author=actor, body=text)
