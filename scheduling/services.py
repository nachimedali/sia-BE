"""Post scheduling (design.md §6.3, §8.5, §8.8, implementation.md Phase 8, 13).

`schedule_post` is the only writer of `Post.delivery_mode`/`Post.
scheduled_at` outside creation (`content/serializers.py` A49 marks both
read-only on `PostSerializer` for exactly this reason) — the horizon check
has to run before either is set, so both writes live behind it rather than
in the view.
"""

from __future__ import annotations

import datetime as dt

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
        post.status = PostStatus.REMINDER_ARMED
        post.save(update_fields=["delivery_mode", "scheduled_at", "status", "updated_at"])
        arm_reminder(post, scheduled_at)

    return post
