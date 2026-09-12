"""The approval state machine (design.md §8.8, I5; BUILD-PLAN C-02, P2-04…P2-11).

```
DRAFT ──submit──> PENDING_REVIEW ──approve, per stage──> APPROVED ──> SCHEDULED ──> PUBLISHED
                        ├──request_changes──> CHANGES_REQUESTED ─┘ (submit again)
                        └──reject──────────> REJECTED
```

**Approval is required on every plan** (L-2, C-02). What a workspace configures
is *which* flow, not *whether* — see `ApprovalChain`. On a non-blocking chain,
scheduling **is** the approval: `approve_implicitly` records an `APPROVE` row
naming whoever scheduled the post, so this invariant holds at every tier and is
testable as one statement —

    no post ever reaches SCHEDULED without an APPROVE action

— rather than as a survey of tiers and flags.

**No new statuses.** `PENDING_REVIEW` means "at stage N"; `Post.current_stage`
says which, and advancing is internal. Adding `PENDING_REVIEW_STAGE_2` would put
the chain's shape into an enum, where every new chain shape is a migration.

**Authorization is split, deliberately.** The `approve` *permission* is a view
gate (`HasPermission`); being on **this stage's** approver list is a property of
the post's current position and can only be answered here, so `_check_stage_authority`
lives in this module and raises 403 rather than returning False.

**Error codes** (P2-08), distinguishable in tests:

| situation                                   | code |
|---------------------------------------------|------|
| illegal transition from the current status   | 409  |
| naming a stage the post has already left     | 409  |
| lacking the `approve` permission             | 403  |
| not on this stage's approver list            | 403  |
| approving your own post where forbidden      | 403  |
| editing a locked post                        | 409  |

403 rather than 402 throughout: no upgrade fixes "you are not one of the named
reviewers".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from common.exceptions import StateConflict
from content.models import Post, PostStatus
from notifications import services as notifications
from notifications.models import EventKey
from workspaces.models import (
    ApprovalAction,
    ApprovalActionType,
    ApprovalChain,
    ApprovalStage,
    AuditLog,
    Permission,
    Workspace,
)
from workspaces.permissions import member_permissions

if TYPE_CHECKING:
    from accounts.models import User

#: The name every workspace's seeded chain carries. A constant because the
#: provisioning service, the migration and the tests all have to agree on it.
DEFAULT_CHAIN_NAME = "Default"

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


class PostLockedError(StateConflict):
    """409. An approved post is frozen until someone with `admin` unlocks it
    (P2-11) — otherwise "approved" describes content nobody approved."""

    default_code = "post_locked"
    default_detail = "This post is locked; it was approved and cannot be edited."


class StageConflictError(StateConflict):
    """409. The named stage is not where the post is.

    Its own code because the fix is different from every other 409 here:
    reload and look again. A client rendering a three-stage chain posts the
    stage id it drew, and by then a colleague may have advanced the post —
    approving "whatever stage it is now" would silently approve a stage nobody
    read.
    """

    default_code = "stage_conflict"
    default_detail = "This post is no longer at that approval stage."


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


# -----------------------------------------------------------------------------
# Chains
# -----------------------------------------------------------------------------
def default_chain(workspace: Workspace) -> ApprovalChain:
    """This workspace's review flow, creating the seeded one if it is missing.

    Self-healing rather than raising: a workspace with no chain has no defined
    answer to "who says yes", and every caller would otherwise have to invent
    one. `get_or_create` also makes the provisioning path and the migration
    idempotent against each other.

    The seeded chain is **non-blocking** — the solo user's flow, where
    scheduling is the approval. Turning it on is one PATCH.
    """
    chain, _created = ApprovalChain.objects.get_or_create(
        workspace=workspace,
        is_default=True,
        defaults={"name": DEFAULT_CHAIN_NAME, "blocks_publish": False},
    )
    return chain


@transaction.atomic
def append_stage(
    chain: ApprovalChain, *, required_approvers: Sequence[Any] = (), **fields: Any
) -> ApprovalStage:
    """Adds a stage to the end of the chain, order and all.

    **Locked on the chain row** (found in review): a plain `aggregate(Max(
    "order"))` read followed by a separate `create()` let two concurrent
    appends compute the same next `order`, and the second `create()` hit
    `unique_approval_stage_order` as a raw, uncaught `IntegrityError` —
    `common.exceptions`'s handler does not special-case it, so it fell through
    to a bare 500 instead of either succeeding in turn or failing with a
    proper error. `select_for_update` serialises the two requests exactly the
    way `content.services.revisions.record` already serialises two concurrent
    saves of the same post on its `sequence` number — the same shape, the same
    reason.

    The depth check moves inside the lock too: checking it before acquiring
    the lock would leave a window where two concurrent appends both read the
    same count and both pass, landing one stage over the plan's ceiling.
    """
    from billing.services.entitlements import entitlements_for

    ApprovalChain.objects.select_for_update().get(pk=chain.pk)
    entitlements_for(chain.workspace).require_chain_depth(chain.stages.count())

    stage = ApprovalStage.objects.create(
        chain=chain,
        order=(chain.stages.aggregate(Max("order"))["order__max"] or 0) + 1,
        **fields,
    )
    stage.required_approvers.set(required_approvers)
    return stage


def first_stage(chain: ApprovalChain) -> ApprovalStage | None:
    return chain.stages.order_by("order").first()


def next_stage(stage: ApprovalStage) -> ApprovalStage | None:
    return stage.chain.stages.filter(order__gt=stage.order).order_by("order").first()


# -----------------------------------------------------------------------------
# Transitions
# -----------------------------------------------------------------------------
def _reviewers(post: Post) -> list[Any]:
    """Who should hear that this post is waiting.

    The current stage's named approvers when it has any; otherwise everyone in
    the workspace holding `approve`. Falling back to the permission rather than
    to silence matters: a single-stage chain names nobody on purpose, and an
    approval request that reaches no inbox is a post that never goes out.
    """
    from django.contrib.auth import get_user_model

    stage = post.current_stage
    if stage is not None:
        named = list(stage.required_approvers.all())
        if named:
            return named

    members = get_user_model().objects.filter(memberships__workspace=post.workspace)
    return [
        member
        for member in members
        if Permission.APPROVE in member_permissions(member, post.workspace)
    ]


def _record(
    post: Post,
    *,
    action: str,
    actor: User | None,
    note: str = "",
    stage: ApprovalStage | None = None,
    guest_link: Any = None,
    required_permission: str = "",
) -> ApprovalAction:
    row = ApprovalAction.objects.create(
        post=post,
        actor=actor,
        guest_link=guest_link,
        action=action,
        stage=stage,
        note=note,
        required_permission=required_permission,
    )
    log(
        workspace=post.workspace,
        actor=actor,
        verb=f"post.{action.lower()}",
        target_repr=str(post),
        meta={"post": post.pk, "note": note, "stage": stage.pk if stage else None},
    )
    return row


def _require_transition(post: Post, action: str) -> str:
    from_statuses, to_status = _TRANSITIONS[action]
    if post.status not in from_statuses:
        raise StateConflict(
            f"Cannot {action.lower()} a post in {post.status} status.",
            detail={"post": post.pk, "status": post.status, "action": action},
        )
    return to_status


@transaction.atomic
def submit_for_review(
    post: Post,
    *,
    actor: User,
    note: str = "",
    delivery_mode: str = "",
    scheduled_at: dt.datetime | None = None,
) -> Post:
    """`DRAFT`/`CHANGES_REQUESTED` → `PENDING_REVIEW`, parked at the first stage.

    `delivery_mode` and `scheduled_at` are the author's **proposal** (P2-10),
    not a schedule: they are stored on the post and consumed by
    `_finalise_approval`, which calls the real schedule service. The author
    picks when it should go out; the reviewer says yes; nobody has to come back
    afterwards to press a second button.

    They are deliberately *not* written to `Post.scheduled_at` here — that
    column has exactly one writer (`scheduling.services.schedule_post`), and a
    proposal that wrote it directly would bypass the horizon, quota and
    entitlement checks that live behind it.
    """
    to_status = _require_transition(post, ApprovalActionType.SUBMIT)
    chain = default_chain(post.workspace)

    post.status = to_status
    post.current_stage = first_stage(chain) if chain.blocks_publish else None
    fields = ["status", "current_stage", "updated_at"]
    if delivery_mode:
        post.proposed_delivery_mode = delivery_mode
        post.proposed_scheduled_at = scheduled_at
        fields += ["proposed_delivery_mode", "proposed_scheduled_at"]
    post.save(update_fields=fields)

    _record(
        post, action=ApprovalActionType.SUBMIT, actor=actor, note=note, stage=post.current_stage
    )
    # Explicit, not a signal (Part 7 rule 8). Fan-out at the event site is what
    # keeps "why did I get this email" answerable by reading this function.
    notifications.post_event(
        post,
        event_key=EventKey.POST_SUBMITTED,
        actor=actor,
        recipients=_reviewers(post),
    )
    return post


def _check_stage_authority(post: Post, stage: ApprovalStage, actor: User) -> None:
    """403 when this person may not clear *this* stage.

    Two separate refusals, both authority rather than state, and neither
    fixable by an upgrade:

    * not on the stage's approver list — when the list is empty the stage means
      "anyone holding `approve`", which the view gate already checked;
    * approving your own post where the stage forbids it, which is the failure
      a review stage exists to prevent.
    """
    approvers = list(stage.required_approvers.values_list("pk", flat=True))
    if approvers and actor.pk not in approvers:
        raise PermissionDenied("You are not one of this stage's approvers.")
    if not stage.allow_self_approve and post.author_id == actor.pk:
        raise PermissionDenied("You cannot approve your own post at this stage.")


@transaction.atomic
def approve(
    post: Post, *, actor: User, note: str = "", stage: ApprovalStage | int | None = None
) -> Post:
    """Clear the post's current stage, and finish the chain if that was the last.

    `stage` is the caller's belief about where the post is. Passing it is
    optional and passing a stale one is a **409**, not a silent approval of
    whatever stage the post has since moved to.
    """
    _require_transition(post, ApprovalActionType.APPROVE)

    current = post.current_stage
    if stage is not None:
        wanted = stage.pk if isinstance(stage, ApprovalStage) else int(stage)
        if current is None or current.pk != wanted:
            raise StageConflictError(
                detail={
                    "post": post.pk,
                    "expected": wanted,
                    "current": current.pk if current else None,
                }
            )

    if current is None:
        # A non-blocking chain, or a blocking one with no stages configured.
        # One approval finishes it.
        _record(
            post,
            action=ApprovalActionType.APPROVE,
            actor=actor,
            note=note,
            required_permission=Permission.APPROVE,
        )
        return _finalise_approval(post, scheduler=actor)

    _check_stage_authority(post, current, actor)
    _record(
        post,
        action=ApprovalActionType.APPROVE,
        actor=actor,
        note=note,
        stage=current,
        required_permission=Permission.APPROVE,
    )
    return _advance(post, stage=current, scheduler=actor)


def _advance(post: Post, *, stage: ApprovalStage, scheduler: User | None) -> Post:
    """Clear the stage if it has enough sign-offs, else leave the post on it.

    Shared by the member and guest paths on purpose. Two copies of "has this
    stage cleared" is how a client-facing approval and an internal one end up
    disagreeing about a two-signature stage.
    """
    cleared = (
        ApprovalAction.objects.filter(post=post, action=ApprovalActionType.APPROVE, stage=stage)
        # Distinct **signatory**, not distinct row. Counted over both columns
        # because a guest has no `actor`: grouping on `actor` alone would make
        # every client who ever signed off count as one person.
        .values("actor", "guest_link")
        .distinct()
        .count()
    )
    if cleared < stage.min_approvals:
        # Still at this stage, and still `PENDING_REVIEW` — the chain's shape
        # lives in the stage rows, never in the status enum.
        return post

    following = next_stage(stage)
    if following is not None:
        post.current_stage = following
        post.save(update_fields=["current_stage", "updated_at"])
        return post

    return _finalise_approval(post, scheduler=scheduler)


@transaction.atomic
def approve_as_guest(post: Post, *, link: Any) -> Post:
    """A client signing off from an emailed `APPROVE` link (P2-09).

    **The link is the delegation.** A member chose this reviewer and sent them
    this post, so the guest clears whatever stage the post is on without being
    named in `required_approvers` — a list they could not be on, having no
    account to be listed by.

    The schedule that may follow is attributed to `link.created_by`, the member
    who proposed the time: the client approved the *content*, and a person with
    no account cannot be the one who spends a workspace's quota. With no such
    member left, the proposal stays on the post for someone to action rather
    than being scheduled by nobody.
    """
    _require_transition(post, ApprovalActionType.APPROVE)
    current = post.current_stage
    _record(
        post,
        action=ApprovalActionType.APPROVE,
        actor=None,
        guest_link=link,
        note=f"approved by {link.email}",
        stage=current,
    )
    if current is None:
        return _finalise_approval(post, scheduler=link.created_by)
    return _advance(post, stage=current, scheduler=link.created_by)


def _finalise_approval(post: Post, *, scheduler: User | None) -> Post:
    """The last stage cleared: `APPROVED`, locked, and scheduled if the author
    proposed a time (P2-10, P2-11).

    The schedule goes through `scheduling.services.schedule_post` — **the sole
    writer** of `delivery_mode`/`scheduled_at`. A second write path here would
    skip the horizon check, the quota debit and the entitlement gate, and would
    do it on the one path where nobody is watching a form.

    `scheduler` is who the resulting schedule is attributed to, and `None`
    means "leave the proposal for a member to action" — the guest path's answer,
    since a person with no account cannot be the one who spent a workspace's
    quota.

    Imported inside the function: `scheduling.services` imports this module for
    the approval gate, so a module-level import would be a cycle.
    """
    from scheduling.services import schedule_post

    post.status = PostStatus.APPROVED
    post.current_stage = None
    post.locked_at = timezone.now()
    post.save(update_fields=["status", "current_stage", "locked_at", "updated_at"])
    notifications.post_event(
        post, event_key=EventKey.POST_APPROVED, actor=scheduler, recipients=[post.author]
    )

    if (
        scheduler is not None
        and post.proposed_delivery_mode
        and post.proposed_scheduled_at
        # **Not silently scheduled once it has gone stale** (found in review).
        # A multi-stage chain can sit `PENDING_REVIEW` for days; `schedule_
        # post` only ever bounded the proposed time from above
        # (`require_scheduling_horizon`), never checked it was still in the
        # future, so a slow chain could clear onto a moment already past —
        # which the beat scan then treats as due immediately, publishing (or
        # reminding) at an instant nobody chose. Left on the post rather than
        # cleared, the same shape `scheduler is None` already uses for "not
        # scheduled yet, and that is deliberate": the approval itself is still
        # legitimate, but the proposal needs a human to pick a new time.
        and post.proposed_scheduled_at > timezone.now()
    ):
        mode, when = post.proposed_delivery_mode, post.proposed_scheduled_at
        post.proposed_delivery_mode = ""
        post.proposed_scheduled_at = None
        post.save(update_fields=["proposed_delivery_mode", "proposed_scheduled_at", "updated_at"])
        post = schedule_post(post=post, delivery_mode=mode, scheduled_at=when, actor=scheduler)
    return post


@transaction.atomic
def request_changes(post: Post, *, actor: User, note: str) -> Post:
    return _reject_like(post, action=ApprovalActionType.REQUEST_CHANGES, actor=actor, note=note)


@transaction.atomic
def reject(post: Post, *, actor: User, note: str = "") -> Post:
    return _reject_like(post, action=ApprovalActionType.REJECT, actor=actor, note=note)


def _reject_like(post: Post, *, action: str, actor: User, note: str) -> Post:
    """Both refusals end the review wherever it had got to.

    `current_stage` is cleared rather than kept: a post sent back for changes
    re-enters at stage 1 when it is resubmitted, because the earlier stages
    approved text that no longer exists.
    """
    to_status = _require_transition(post, action)
    if post.current_stage is not None:
        _check_stage_authority(post, post.current_stage, actor)

    stage = post.current_stage
    post.status = to_status
    post.current_stage = None
    post.save(update_fields=["status", "current_stage", "updated_at"])
    _record(post, action=action, actor=actor, note=note, stage=stage)
    notifications.post_event(
        post,
        event_key=(
            EventKey.POST_CHANGES_REQUESTED
            if action == ApprovalActionType.REQUEST_CHANGES
            else EventKey.POST_REJECTED
        ),
        actor=actor,
        recipients=[post.author],
    )
    return post


def approve_implicitly(post: Post, *, actor: User) -> Post:
    """The non-blocking chain's approval: **scheduling is saying yes** (L-2).

    Called only by `scheduling.services.schedule_post`, and only when the
    workspace's default chain does not block. A human with `publish` chose this
    post and this time; that is a decision, and it gets a row like any other.

    Deliberately not routed through `approve` above: there is no transition to
    check (the post is a `DRAFT`, which `approve` rightly refuses) and no stage
    authority to test (there is no stage). Reusing it would mean widening
    `_TRANSITIONS` so that `DRAFT → APPROVED` is legal generally, which would
    make the explicit review path skippable by anyone who called `approve`
    early.
    """
    _record(
        post,
        action=ApprovalActionType.APPROVE,
        actor=actor,
        note="approved at schedule (open chain)",
        required_permission=Permission.PUBLISH,
    )
    post.status = PostStatus.APPROVED
    post.save(update_fields=["status", "updated_at"])
    return post


# -----------------------------------------------------------------------------
# Locking (P2-11)
# -----------------------------------------------------------------------------
def ensure_unlocked(post: Post) -> None:
    """409 on any mutation of an approved post.

    Called from `content.services.posts` and `content.services.revisions` — the
    **service layer**, not the view — so a task, a management command or a
    future endpoint inherits the check instead of having to remember it.

    Revisions stay readable throughout: history is not a mutation, and locking
    people out of what a post used to say helps nobody.
    """
    if post.locked_at is not None:
        raise PostLockedError(detail={"post": post.pk, "locked_at": post.locked_at.isoformat()})


@transaction.atomic
def unlock(post: Post, *, actor: User) -> Post:
    """Reopen an approved post for editing. Requires `admin` at the view gate.

    Writes an audit entry, because unlocking is how approved content becomes
    editable again and "who reopened this" is exactly the question asked after
    something wrong goes out.

    The post keeps its `APPROVED` status rather than dropping to `DRAFT`:
    unlocking is not a rejection, and the next content edit is what voids the
    approval (`content.services.posts.update_post`).
    """
    post.locked_at = None
    post.save(update_fields=["locked_at", "updated_at"])
    log(
        workspace=post.workspace,
        actor=actor,
        verb="post.unlocked",
        target_repr=str(post),
        meta={"post": post.pk},
    )
    return post


# -----------------------------------------------------------------------------
# The Celery-preflight recheck (I5)
# -----------------------------------------------------------------------------
class ApprovalRevokedError(StateConflict):
    """The Celery-preflight recheck (I5): whoever approved this post no
    longer holds the role that let them. Publishing must not proceed on an
    approval that would not be granted again today."""

    default_code = "approval_revoked"
    default_detail = "The approver no longer has permission to approve posts in this workspace."


def ensure_approval_still_valid(post: Post) -> None:
    """Re-verifies, at publish time, that the post's most recent approver still
    holds the authority they used — "a role revoked between scheduling and
    execution must block the publish" (design.md §8.8).

    Called from `scheduling.publishing.preflight`, which is what makes it run
    on every publish attempt rather than only the first.

    Three rows are exempt, and none is a loophole:

    * a **grandfathered** approval (P2-06) has no person whose authority could
      have been revoked;
    * a **guest** approval (P2-09) was granted by someone with no membership,
      so there is no permission to re-read — what can be withdrawn there is the
      link, and `review.resolve` already refuses a revoked one;
    * an **implicit** approval on a non-blocking chain was granted by whoever
      scheduled the post, so the authority to re-check is `publish`, not
      `approve` — asking for `approve` would block every EDITOR-scheduled post
      in a workspace that never turned review on.
    """
    latest = (
        ApprovalAction.objects.filter(post=post, action=ApprovalActionType.APPROVE)
        .select_related("actor")
        .order_by("-created_at")
        .first()
    )
    if latest is None or latest.actor is None:
        return

    # Fixed at the moment the approval was recorded (found in review: this
    # used to be recomputed from the chain's *current* `blocks_publish`, so an
    # admin toggling that setting after the fact silently changed which
    # authority a past approval is judged against). A blank value only reaches
    # here for a row written before this field existed — this codebase has
    # never deployed, so that is a theoretical case, not a live one, and the
    # old recompute is the correct fallback for it rather than a crash.
    required = latest.required_permission or (
        Permission.APPROVE if default_chain(post.workspace).blocks_publish else Permission.PUBLISH
    )
    if required not in member_permissions(latest.actor, post.workspace):
        raise ApprovalRevokedError(
            detail={"post": post.pk, "approver": latest.actor_id, "permission": required}
        )
