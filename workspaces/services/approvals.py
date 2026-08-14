"""The approval state machine (design.md §8.8, I5; implementation.md Phase 13).

```
DRAFT ──submit──> PENDING_REVIEW ──approve──────────> APPROVED ──> SCHEDULED ──> PUBLISHED
                        ├──request_changes──> CHANGES_REQUESTED ─┘ (submit again)
                        └──reject──────────> REJECTED
```

`APPROVED ──> SCHEDULED ──> PUBLISHED` carries no action name because nothing
here drives it — `scheduling.services.schedule_post` and Phase 9's publish
path already own those transitions; this module's only interest in them is
`ensure_approval_still_valid`, the recheck `scheduling.publishing.preflight`
calls before a scheduled post is actually sent (see its docstring for why).

**Role authorization is a view-layer concern, not a service one.**
`workspaces.permissions.HasRole` gates who may call `approve`/`request_changes`
/`reject` (ADMIN+) and `submit_for_review`/comments (anyone but VIEWER); this
module only enforces the state machine itself — that a transition is legal
from the post's *current* status, regardless of who is asking. The one place
staleness between "was authorized" and "still is" actually matters is days
later, at publish time, which is exactly what `ensure_approval_still_valid`
re-checks (I5's Celery-preflight half) rather than anything in this file's
transitions themselves.

**Illegal transitions are 409, not a silent no-op** (design.md §8.8) — reusing
`common.exceptions.StateConflict`, declared since Phase 1 for exactly this.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from common.exceptions import StateConflict
from content.models import Post, PostStatus
from workspaces.models import (
    ROLE_RANK,
    ApprovalAction,
    ApprovalActionType,
    AuditLog,
    Membership,
    PostComment,
    Role,
)

#: `from_statuses` per action, keyed the same way `ApprovalActionType` names
#: the transition rather than the state it lands on.
_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    ApprovalActionType.SUBMIT: (
        frozenset({PostStatus.DRAFT, PostStatus.CHANGES_REQUESTED}),
        PostStatus.PENDING_REVIEW,
    ),
    ApprovalActionType.APPROVE: (
        frozenset({PostStatus.PENDING_REVIEW}),
        PostStatus.APPROVED,
    ),
    ApprovalActionType.REQUEST_CHANGES: (
        frozenset({PostStatus.PENDING_REVIEW}),
        PostStatus.CHANGES_REQUESTED,
    ),
    ApprovalActionType.REJECT: (
        frozenset({PostStatus.PENDING_REVIEW}),
        PostStatus.REJECTED,
    ),
}


def log(
    *,
    workspace: Any,
    actor: Any = None,
    verb: str,
    target_repr: str = "",
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    """The one writer of `AuditLog` — so "who did what" has exactly one place
    that can get the shape wrong, the same reason `billing.services.ledger` is
    the sole writer of the two ledgers."""
    return AuditLog.objects.create(
        workspace=workspace, actor=actor, verb=verb, target_repr=target_repr, meta=meta or {}
    )


@transaction.atomic
def _transition(post: Post, *, action: str, actor: Any, note: str = "") -> Post:
    from_statuses, to_status = _TRANSITIONS[action]
    if post.status not in from_statuses:
        raise StateConflict(
            f"Cannot {action.lower()} a post in {post.status} status.",
            detail={"post": post.pk, "status": post.status, "action": action},
        )

    ApprovalAction.objects.create(post=post, actor=actor, action=action, note=note)
    post.status = to_status
    post.save(update_fields=["status", "updated_at"])
    log(
        workspace=post.workspace,
        actor=actor,
        verb=f"post.{action.lower()}",
        target_repr=str(post),
        meta={"post": post.pk, "note": note},
    )
    return post


def submit_for_review(post: Post, *, actor: Any, note: str = "") -> Post:
    return _transition(post, action=ApprovalActionType.SUBMIT, actor=actor, note=note)


def approve(post: Post, *, actor: Any, note: str = "") -> Post:
    return _transition(post, action=ApprovalActionType.APPROVE, actor=actor, note=note)


def request_changes(post: Post, *, actor: Any, note: str) -> Post:
    return _transition(post, action=ApprovalActionType.REQUEST_CHANGES, actor=actor, note=note)


def reject(post: Post, *, actor: Any, note: str = "") -> Post:
    return _transition(post, action=ApprovalActionType.REJECT, actor=actor, note=note)


def add_comment(
    post: Post, *, author: Any, body: str, parent: PostComment | None = None
) -> PostComment:
    return PostComment.objects.create(post=post, author=author, body=body, parent=parent)


def resolve_comment(comment: PostComment) -> PostComment:
    """Idempotent: resolving an already-resolved comment a second time is not
    an error, since two people clicking "resolve" at once is an ordinary race,
    not a conflict worth surfacing."""
    if comment.resolved_at is None:
        comment.resolved_at = timezone.now()
        comment.save(update_fields=["resolved_at", "updated_at"])
    return comment


class ApprovalRevokedError(StateConflict):
    """The Celery-preflight recheck (I5): whoever approved this post no
    longer holds the role that let them. Publishing must not proceed on an
    approval that would not be granted again today."""

    default_code = "approval_revoked"
    default_detail = "The approver no longer has permission to approve posts in this workspace."


def ensure_approval_still_valid(post: Post) -> None:
    """Re-verifies, at publish time, that the post's most recent approver
    still holds ADMIN+ in this workspace — "a role revoked between scheduling
    and execution must block the publish" (design.md §8.8).

    A no-op when the approval workflow was never active for this workspace:
    a post that reached `SCHEDULED` without ever needing an `APPROVE` row has
    nothing here to re-verify. Called from `scheduling.publishing.preflight`,
    which is what makes it run on every publish attempt, not just the first.
    """
    if not post.workspace.requires_approval:
        return

    latest_approval = (
        ApprovalAction.objects.filter(post=post, action=ApprovalActionType.APPROVE)
        .order_by("-created_at")
        .first()
    )
    if latest_approval is None:
        # Scheduled before the workspace turned approval on, or some other
        # path that never went through this state machine — nothing to
        # revoke, so nothing to block.
        return

    role = (
        Membership.objects.filter(user=latest_approval.actor, workspace=post.workspace)
        .values_list("role", flat=True)
        .first()
    )
    if role is None or ROLE_RANK[role] > ROLE_RANK[Role.ADMIN]:
        raise ApprovalRevokedError(detail={"post": post.pk, "approver": latest_approval.actor_id})
