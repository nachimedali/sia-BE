"""L-2 as one testable statement (C-02, P2-04, P2-05, P2-06).

    No post ever reaches a scheduled state without an APPROVE action.

Not "on Advanced", not "when the toggle is on" — always. The value of stating
it this way is that it can be checked exhaustively across plans and
configurations in a loop, instead of as a survey of tiers that someone has to
remember to extend when a plan is added.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from billing.models import FeatureFlag
from billing.services.flags import COLLABORATION_V2
from common.exceptions import StateConflict
from content.models import PostStatus
from content.services.posts import create_post
from scheduling.services import schedule_post
from workspaces.models import ApprovalAction, ApprovalActionType
from workspaces.services import approvals

pytestmark = pytest.mark.django_db

#: Every status that means "this post has been committed to going out". The
#: same list the grandfathering migration uses, and for the same reason.
COMMITTED = frozenset(
    {
        PostStatus.SCHEDULED,
        PostStatus.REMINDER_ARMED,
        PostStatus.PUBLISHING,
        PostStatus.PUBLISHED,
    }
)


def _approve_rows(post: Any) -> int:
    return ApprovalAction.objects.filter(post=post, action=ApprovalActionType.APPROVE).count()


@pytest.mark.parametrize("code", ["trial", "free", "pro", "advanced"])
def test_scheduling_writes_an_approval_on_every_plan(
    workspace: Any, user: Any, plans: Any, code: str
) -> None:
    """The invariant, swept across the whole price list.

    On an open chain the person scheduling is the approver — no extra clicks
    for a solo user — and the row naming them is what keeps the statement above
    true without exceptions.
    """
    workspace.organization.plan = plans[code]
    workspace.organization.save(update_fields=["plan"])
    post = create_post(workspace=workspace, author=user, master_body="Going out")

    post = schedule_post(
        post=post,
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.status in COMMITTED
    assert _approve_rows(post) == 1
    assert ApprovalAction.objects.get(post=post, action="APPROVE").actor_id == user.pk


def test_no_committed_post_anywhere_lacks_an_approval(
    workspace: Any, user: Any, other_user: Any, plans: Any
) -> None:
    """The same statement checked over a mixed corpus rather than one post:
    **both chain shapes, in the same database**, with nothing exempted.

    Two separately provisioned workspaces rather than the `advanced_workspace`
    fixture, which mutates `workspace` in place — one row cannot have an open
    chain and a blocking one at the same time, and a test that thought it did
    would be asserting over a corpus of one.
    """
    from content.models import Post
    from workspaces.services.provisioning import provision_workspace

    reviewed = provision_workspace(other_user, name="Reviewed Brand")
    reviewed_chain = approvals.default_chain(reviewed)
    reviewed_chain.blocks_publish = True
    reviewed_chain.save(update_fields=["blocks_publish"])

    open_chain_post = schedule_post(
        post=create_post(workspace=workspace, author=user, master_body="Open chain"),
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )
    blocking = create_post(workspace=reviewed, author=other_user, master_body="Blocking chain")
    blocking = approvals.submit_for_review(blocking, actor=other_user)
    blocking = approvals.approve(blocking, actor=other_user)
    blocking = schedule_post(
        post=blocking,
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=other_user,
    )

    committed = Post.objects.filter(status__in=COMMITTED)

    assert set(committed.values_list("pk", flat=True)) == {open_chain_post.pk, blocking.pk}
    for post in committed:
        assert _approve_rows(post) >= 1, f"post {post.pk} reached {post.status} unapproved"


def test_an_open_chain_costs_the_solo_user_no_extra_click(workspace: Any, user: Any) -> None:
    """The reason the invariant is affordable. A `DRAFT` goes straight to
    scheduled — no submit, no self-review — and still leaves a row."""
    post = create_post(workspace=workspace, author=user, master_body="Just ship it")
    assert post.status == PostStatus.DRAFT

    post = schedule_post(
        post=post,
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.status == PostStatus.REMINDER_ARMED
    assert _approve_rows(post) == 1


def test_a_blocking_chain_refuses_an_unapproved_post(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Not yet")

    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="REMINDER",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            actor=admin_user,
        )

    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT
    assert _approve_rows(post) == 0


def test_auto_publish_is_reachable_only_from_approved(
    workspace: Any, user: Any, paid_workspace: Any, social_account: Any
) -> None:
    """P2-05. `AUTO_PUBLISH` keeps its meaning — publish at the scheduled
    minute — and is reached only after the approval above it."""
    post = create_post(workspace=paid_workspace, author=user, master_body="Auto")

    post = schedule_post(
        post=post,
        delivery_mode="AUTO_PUBLISH",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.status == PostStatus.SCHEDULED
    trail = list(
        ApprovalAction.objects.filter(post=post)
        .order_by("created_at")
        .values_list("action", flat=True)
    )
    assert trail == ["APPROVE"]


# -----------------------------------------------------------------------------
# Part 3 — flag off is pre-phase behaviour, not an error
# -----------------------------------------------------------------------------
def test_with_the_flag_off_an_open_chain_records_nothing(
    workspace: Any, user: Any, organization: Any
) -> None:
    """Before Phase 2 a workspace that had not switched approval on scheduled
    straight from `DRAFT` with no record. That is what off restores — a working
    prior behaviour, not an error."""
    FeatureFlag.objects.create(organization=organization, key=COLLABORATION_V2, enabled=False)
    post = create_post(workspace=workspace, author=user, master_body="Pre-phase")

    post = schedule_post(
        post=post,
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.status == PostStatus.REMINDER_ARMED
    assert _approve_rows(post) == 0


def test_a_blocking_chain_is_honoured_with_the_flag_off_too(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """The flag governs the *universal* half only. A workspace that asked for a
    separate reviewer had that before Phase 2, so turning the phase off must
    not hand it back an ungated calendar."""
    FeatureFlag.objects.create(
        organization=advanced_workspace.organization, key=COLLABORATION_V2, enabled=False
    )
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Still gated"
    )

    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="REMINDER",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            actor=admin_user,
        )


# -----------------------------------------------------------------------------
# The publish-time recheck (I5), now that approvals have two shapes
# -----------------------------------------------------------------------------
def test_an_open_chain_approval_is_rechecked_against_publish_not_approve(
    workspace: Any, user: Any
) -> None:
    """An implicit approval was granted by whoever *scheduled* it, so `publish`
    is the authority to re-verify. Asking for `approve` would block every
    EDITOR-scheduled post in a workspace that never turned review on."""
    from workspaces.models import Membership, Role

    post = schedule_post(
        post=create_post(workspace=workspace, author=user, master_body="Open"),
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )
    membership = Membership.objects.get(user=user, workspace=workspace)
    membership.role = Role.EDITOR
    membership.permissions = ["view", "comment", "edit", "publish", "analyze"]
    membership.save(update_fields=["role", "permissions"])

    approvals.ensure_approval_still_valid(post)  # holds `publish`, not `approve`

    membership.permissions = ["view", "analyze"]
    membership.save(update_fields=["permissions"])

    with pytest.raises(approvals.ApprovalRevokedError):
        approvals.ensure_approval_still_valid(post)


def test_a_grandfathered_approval_has_no_authority_to_revoke(workspace: Any, user: Any) -> None:
    """P2-06's rows name nobody, so there is no person whose authority could
    have been withdrawn. Skipped rather than treated as a failure — a
    migration's own row must not start failing publishes."""
    post = create_post(workspace=workspace, author=user, master_body="Grandfathered")
    post.status = PostStatus.SCHEDULED
    post.save(update_fields=["status"])
    ApprovalAction.objects.create(
        post=post, actor=None, action=ApprovalActionType.APPROVE, note="grandfathered"
    )

    approvals.ensure_approval_still_valid(post)
