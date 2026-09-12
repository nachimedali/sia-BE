"""Post scheduling (design.md §6.3, §8.5, §8.8; BUILD-PLAN C-02, P2-04, P2-05).

`schedule_post` is the only writer of `Post.delivery_mode`/`Post.scheduled_at`
outside creation (`content/serializers.py` A49 marks both read-only on
`PostSerializer` for exactly this reason) — the horizon check has to run before
either is set, so both writes live behind it rather than in the view.

**Since Phase 2 this is also where L-2 is enforced.** Nothing reaches
`SCHEDULED` without an `APPROVE` action naming a person, on any plan, under any
configuration:

* the workspace's default chain **blocks** → the post must already be
  `APPROVED`, or this is a 409;
* the chain **does not block** → scheduling *is* the approval, and an `APPROVE`
  row is written naming whoever scheduled it.

The second branch is what keeps a solo user from having to review their own
draft while still leaving `no SCHEDULED post without an APPROVE row` true as a
single, testable statement.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from billing.services import trial
from billing.services.entitlements import entitlements_for
from billing.services.flags import COLLABORATION_V2, flag_enabled
from common.exceptions import StateConflict
from content.models import ContentKind, DeliveryMode, Post, PostStatus
from reminders.services import arm_reminder
from scheduling.publishing import build_targets
from workspaces.services import approvals

if TYPE_CHECKING:
    from accounts.models import User


def _gate_approval(post: Post, *, actor: User) -> Post:
    """Apply the workspace's chain, and return the post ready to schedule.

    **Flag off is pre-phase behaviour, not an error** (Part 3): before Phase 2
    a workspace that had not switched approval on scheduled straight from
    `DRAFT` with no record, and with `COLLABORATION_V2` off it still does. A
    blocking chain is honoured either way, because that is the behaviour the
    old `requires_approval=True` already had.
    """
    if post.status == PostStatus.APPROVED:
        return post

    if approvals.default_chain(post.workspace).blocks_publish:
        raise StateConflict(
            "This workspace requires approval before a post can be scheduled.",
            detail={"post": post.pk, "status": post.status},
        )

    if not flag_enabled(post.workspace.organization, COLLABORATION_V2):
        return post

    return approvals.approve_implicitly(post, actor=actor)


def schedule_post(
    *, post: Post, delivery_mode: str, scheduled_at: dt.datetime, actor: User
) -> Post:
    """`actor` is **required**, with no default (L-2).

    Someone always did this, and a default of `None` would be a quiet way for a
    future caller to schedule a post with nobody's name on the approval. A
    keyword with no default makes the omission a `TypeError` at the call site
    rather than a null in the audit trail.
    """
    # **A DOC has nowhere to go** (P3-01). It carries no targets and no
    # adaptation, so a scheduled one would sit in the beat scan forever, or —
    # worse — build zero targets and report success. 409 rather than 400: the
    # request is well-formed, it is this post that cannot be in this state.
    if post.content_kind == ContentKind.DOC:
        raise StateConflict(
            "A document is published by hand, not scheduled.",
            detail={"content_kind": post.content_kind},
        )

    entitlements = entitlements_for(post.workspace)
    entitlements.require_scheduling_horizon(scheduled_at)

    post = _gate_approval(post, actor=actor)

    # L-4/P0-20: the quota trial is metered here, at the moment a post is
    # committed to going out, rather than at creation. A draft nobody schedules
    # has cost nothing, and counting it would make the trial feel smaller than
    # it is. Pooled at the organization, so two brands cannot both spend the
    # last post.
    trial.consume_trial_post(post.workspace)

    post.delivery_mode = delivery_mode
    post.scheduled_at = scheduled_at

    if delivery_mode == DeliveryMode.AUTO_PUBLISH:
        # D4: Free tier is reminders-only.
        entitlements.require_feature("auto_publish")
        post.status = PostStatus.SCHEDULED
        post.save(update_fields=["delivery_mode", "scheduled_at", "status", "updated_at"])
        # Targets are built now, not when the post fires: the calendar can
        # then show where it is going before it goes, and the idempotency key
        # each target carries is minted once here rather than by whichever
        # publish attempt happens to run first (I9, scheduling/publishing.py).
        build_targets(post)
    else:
        # **REMINDER carries no entitlement check, on any plan** (L-4, P0-22).
        # It is a capability, not a price point — the only path for formats the
        # publishing API cannot reach — and gating it would leave those formats
        # unreachable rather than merely unpaid. `test_reminder_is_available_on
        # _every_plan` is what keeps a future gate out of this branch.
        post.status = PostStatus.REMINDER_ARMED
        post.save(update_fields=["delivery_mode", "scheduled_at", "status", "updated_at"])
        arm_reminder(post, scheduled_at)

    return post
