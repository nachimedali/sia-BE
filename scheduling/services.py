"""Post scheduling (design.md §6.3, §8.5, §8.8, implementation.md Phase 8, 13).

`schedule_post` is the only writer of `Post.delivery_mode`/`Post.
scheduled_at` outside creation (`content/serializers.py` A49 marks both
read-only on `PostSerializer` for exactly this reason) — the horizon check
has to run before either is set, so both writes live behind it rather than
in the view.
"""

from __future__ import annotations

import datetime as dt

from billing.services import trial
from billing.services.entitlements import entitlements_for
from common.exceptions import StateConflict
from content.models import DeliveryMode, Post, PostStatus
from reminders.services import arm_reminder
from scheduling.publishing import build_targets


def schedule_post(*, post: Post, delivery_mode: str, scheduled_at: dt.datetime) -> Post:
    entitlements = entitlements_for(post.workspace)
    entitlements.require_scheduling_horizon(scheduled_at)

    # design.md §8.8: active when the workspace has switched it on *and* the
    # plan still includes it — re-checked here rather than trusted from the
    # toggle alone, the same reasoning `Entitlements` applies to a lapsed
    # trial: a downgrade must make the requirement inert, not enforce a
    # feature the workspace no longer pays for.
    approval_active = post.workspace.requires_approval and entitlements.feature("approval_workflow")
    if approval_active and post.status != PostStatus.APPROVED:
        raise StateConflict(
            "This workspace requires approval before a post can be scheduled.",
            detail={"post": post.pk, "status": post.status},
        )

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
