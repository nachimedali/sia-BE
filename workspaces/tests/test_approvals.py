"""The approval state machine (design.md §8.8, I5; implementation.md Phase 13)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from common.exceptions import StateConflict
from common.records import AppendOnlyError
from content.models import PostStatus
from content.services.posts import create_post, update_post
from scheduling.publishing import NoConnectedAccountsError
from scheduling.services import schedule_post
from workspaces.models import ApprovalAction, ApprovalActionType, AuditLog
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# The state machine itself
# -----------------------------------------------------------------------------
def test_submit_moves_draft_to_pending_review(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    post = approvals.submit_for_review(post, actor=contributor_user)

    assert post.status == PostStatus.PENDING_REVIEW
    assert ApprovalAction.objects.filter(
        post=post, actor=contributor_user, action=ApprovalActionType.SUBMIT
    ).exists()


def test_submit_also_accepts_changes_requested(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """The second half of the loop the ASCII diagram draws: a post sent back
    for changes can be resubmitted, not just a fresh draft."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.request_changes(post, actor=admin_user, note="Fix the CTA")

    post = approvals.submit_for_review(post, actor=contributor_user)

    assert post.status == PostStatus.PENDING_REVIEW


def test_approve_moves_pending_review_to_approved(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)

    post = approvals.approve(post, actor=admin_user, note="Looks good")

    assert post.status == PostStatus.APPROVED
    action = ApprovalAction.objects.get(post=post, action=ApprovalActionType.APPROVE)
    assert action.actor_id == admin_user.pk
    assert action.note == "Looks good"


def test_reject_moves_pending_review_to_rejected(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)

    post = approvals.reject(post, actor=admin_user, note="Not on-brand")

    assert post.status == PostStatus.REJECTED


def test_request_changes_moves_pending_review_to_changes_requested(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)

    post = approvals.request_changes(post, actor=admin_user, note="Shorter, please")

    assert post.status == PostStatus.CHANGES_REQUESTED


# -----------------------------------------------------------------------------
# test_illegal_state_transition_returns_409 (service-level half; the API-level
# half — that it really is a 409 over HTTP — lives in
# content/tests/test_approvals_api.py)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        (approvals.approve, {}),
        (approvals.reject, {}),
        (approvals.request_changes, {"note": "why"}),
    ],
)
def test_approving_rejecting_or_requesting_changes_on_a_draft_is_illegal(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any, action: Any, kwargs: Any
) -> None:
    """None of the three decision actions may act on a `DRAFT` — only
    `PENDING_REVIEW` is a legal `from_status` for them."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    with pytest.raises(StateConflict) as excinfo:
        action(post, actor=admin_user, **kwargs)

    assert excinfo.value.status_code == 409
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT


def test_submitting_an_already_approved_post_is_illegal(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = approvals.unlock(post, actor=admin_user)

    with pytest.raises(StateConflict):
        approvals.submit_for_review(post, actor=contributor_user)


# -----------------------------------------------------------------------------
# test_editing_approved_post_reverts_to_pending_review
# -----------------------------------------------------------------------------
def test_editing_approved_post_reverts_to_pending_review(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """The two Phase 2 rules meet here, and they meet coherently.

    Approval **locks** the post (P2-11), so the first thing an edit hits is a
    409. An `admin` unlocks, which is audited — and then the pre-existing rule
    applies unchanged: a content edit voids the approval it invalidated.
    """
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    assert post.status == PostStatus.APPROVED

    with pytest.raises(approvals.PostLockedError):
        update_post(post, master_body="Changed my mind about the wording")

    post = approvals.unlock(post, actor=admin_user)
    post = update_post(post, master_body="Changed my mind about the wording")

    assert post.status == PostStatus.PENDING_REVIEW
    assert AuditLog.objects.filter(
        workspace=advanced_workspace, verb="post.edited_after_approval"
    ).exists()


def test_editing_a_field_that_is_not_content_does_not_revert_approval(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any, category: Any
) -> None:
    """`category`/`source`/`product`/`generation` are metadata other phases'
    services write through this same function — none of them is the thing an
    approver signed off on."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = approvals.unlock(post, actor=admin_user)

    post = update_post(post, category=category)

    assert post.status == PostStatus.APPROVED


def test_editing_a_non_approved_post_does_not_write_an_audit_entry(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    update_post(post, master_body="Still a draft")

    assert not AuditLog.objects.filter(verb="post.edited_after_approval").exists()


# -----------------------------------------------------------------------------
# test_contributor_post_cannot_publish_until_admin_approves
# -----------------------------------------------------------------------------
def test_contributor_post_cannot_publish_until_admin_approves(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any, advanced_social_account: Any
) -> None:
    """The whole point of the phase, end to end: a CONTRIBUTOR cannot get a
    post scheduled on their own, and an ADMIN's approval is what unblocks it."""
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="New drop"
    )
    post = approvals.submit_for_review(post, actor=contributor_user)
    scheduled_at = timezone.now() + dt.timedelta(minutes=5)

    # Not yet approved: scheduling — the CONTRIBUTOR's own next move — is
    # refused outright, so there is nothing left that could publish it.
    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=scheduled_at,
            actor=contributor_user,
        )

    post = approvals.approve(post, actor=admin_user)

    scheduled = schedule_post(
        post=post, delivery_mode="AUTO_PUBLISH", scheduled_at=scheduled_at, actor=admin_user
    )

    assert scheduled.status == PostStatus.SCHEDULED


def test_a_reminder_delivery_is_gated_the_same_way_auto_publish_is(
    advanced_workspace: Any, contributor_user: Any
) -> None:
    """§8.8's gate is not auto-publish-specific: a workspace that requires
    approval wants everything reviewed before it goes out, reminder or not."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="REMINDER",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
            actor=contributor_user,
        )


def test_a_downgrade_does_not_switch_the_approval_gate_off(
    advanced_workspace: Any, contributor_user: Any, advanced_social_account: Any, plans: Any
) -> None:
    """**The opposite of what this asserted before C-02.**

    A blocking chain used to be inert on a plan without `approval_workflow`,
    because approval was an Advanced feature and a downgrade had to make the
    feature stop applying. Approval is now universal (L-2): a downgrade may
    remove chain *depth*, and it may never remove the requirement that a human
    said yes. A gate that a billing change can switch off is not a gate.
    """
    advanced_workspace.organization.plan = plans["pro"]
    advanced_workspace.organization.save(update_fields=["plan"])
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")

    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
            actor=contributor_user,
        )


def test_auto_publish_scheduling_without_a_connected_account_still_refuses(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """The approval gate runs first, but does not replace Phase 9's own
    precondition — an approved post with nowhere to publish still 4xx's."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)

    with pytest.raises(NoConnectedAccountsError):
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(minutes=5),
            actor=admin_user,
        )


# -----------------------------------------------------------------------------
# test_approval_action_and_audit_log_are_append_only
# -----------------------------------------------------------------------------
def test_approval_action_and_audit_log_are_append_only(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    action = approvals.approve(post, actor=admin_user).approval_actions.get(
        action=ApprovalActionType.APPROVE
    )
    entry = approvals.log(workspace=advanced_workspace, actor=admin_user, verb="test.verb")

    with pytest.raises(AppendOnlyError):
        action.note = "rewritten after the fact"
        action.save()
    with pytest.raises(AppendOnlyError):
        action.delete()

    with pytest.raises(AppendOnlyError):
        entry.verb = "rewritten"
        entry.save()
    with pytest.raises(AppendOnlyError):
        entry.delete()

    # Untouched: the guard raised before either write reached the row.
    action.refresh_from_db()
    entry.refresh_from_db()
    assert action.note != "rewritten after the fact"
    assert entry.verb == "test.verb"


# -----------------------------------------------------------------------------
# ensure_approval_still_valid — unit coverage; the full Celery-preflight path
# is scheduling/tests/test_publishing.py::
# test_role_revoked_after_scheduling_blocks_publish (the named Phase 13 test).
# -----------------------------------------------------------------------------
def test_ensure_approval_still_valid_is_a_noop_when_approval_was_never_active(
    workspace: Any, user: Any
) -> None:
    """`workspace.requires_approval` is `False` by default — a post scheduled
    under a plan/workspace that never required approval has no `APPROVE` row
    to re-check, and must not be treated as revoked."""
    post = create_post(workspace=workspace, author=user, master_body="Draft")

    approvals.ensure_approval_still_valid(post)  # must not raise


def test_ensure_approval_still_valid_passes_while_the_approver_still_holds_admin(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)

    approvals.ensure_approval_still_valid(post)  # must not raise


def test_ensure_approval_still_valid_is_unaffected_by_the_chain_blocking_afterward(
    workspace: Any, user: Any
) -> None:
    """Found in review: the check used to recompute `required` from
    `default_chain(post.workspace).blocks_publish` **read fresh at publish
    time**, rather than from what was actually true when the approval
    happened. An admin turning review on after an EDITOR's legitimate,
    open-chain approval must not retroactively invalidate that approval —
    the approver's authority did not change; the workspace's policy did.
    """
    from workspaces.models import Membership, Role

    post = schedule_post(
        post=create_post(workspace=workspace, author=user, master_body="Open chain"),
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )
    membership = Membership.objects.get(user=user, workspace=workspace)
    membership.role = Role.EDITOR
    membership.permissions = ["view", "comment", "edit", "publish", "analyze"]
    membership.save(update_fields=["role", "permissions"])

    # The chain starts blocking *after* the approval already happened.
    chain = approvals.default_chain(workspace)
    chain.blocks_publish = True
    chain.save(update_fields=["blocks_publish"])

    approvals.ensure_approval_still_valid(post)  # must not raise


def test_ensure_approval_still_valid_still_catches_a_real_revocation_on_an_open_chain(
    workspace: Any, user: Any
) -> None:
    """The fix must not turn the check into a no-op: revoking the actual
    permission the approver used must still be caught, regardless of the
    chain's current setting."""
    from workspaces.models import Membership, Role

    post = schedule_post(
        post=create_post(workspace=workspace, author=user, master_body="Open chain"),
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )
    membership = Membership.objects.get(user=user, workspace=workspace)
    membership.role = Role.EDITOR
    membership.permissions = ["view", "comment", "edit", "publish", "analyze"]
    membership.save(update_fields=["role", "permissions"])

    # The chain also starts blocking, so a *naive* fix that just always checks
    # `publish` for an open-chain approval would wrongly pass here too if it
    # ignored a genuinely revoked `publish` grant.
    chain = approvals.default_chain(workspace)
    chain.blocks_publish = True
    chain.save(update_fields=["blocks_publish"])

    membership.permissions = ["view", "analyze"]  # publish revoked
    membership.save(update_fields=["permissions"])

    with pytest.raises(approvals.ApprovalRevokedError):
        approvals.ensure_approval_still_valid(post)


def test_ensure_approval_still_valid_is_unaffected_by_the_chain_unblocking_afterward(
    advanced_workspace: Any, contributor_user: Any, admin_user: Any
) -> None:
    """The symmetric case: a real, explicit `approve` (needing `approve`) must
    not be second-guessed against `publish` just because an admin turned
    review off afterward."""
    post = create_post(workspace=advanced_workspace, author=contributor_user, master_body="Draft")
    post = approvals.submit_for_review(post, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)

    chain = approvals.default_chain(advanced_workspace)
    chain.blocks_publish = False
    chain.save(update_fields=["blocks_publish"])

    approvals.ensure_approval_still_valid(post)  # must not raise
