"""Approval chains (P2-07, P2-08, P2-10, P2-13).

Two things are being pinned here, and they are different in kind:

* **Modes are configurations, not code paths.** One state machine reads the
  stage rows. The multi-stage tests below drive the *same* `approve` call the
  single-stage ones do; nothing branches on "which mode is this".
* **Every emittable error code is distinguishable**, because the four ways an
  approval can be refused lead to four different fixes and a client that cannot
  tell them apart will show the wrong one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from content.models import PostStatus
from content.services.posts import create_post
from workspaces.models import ApprovalAction, ApprovalStage, Membership, Role
from workspaces.services import approvals

pytestmark = pytest.mark.django_db

CHAIN_URL = "/api/v1/workspaces/approval-chain/"
SETTINGS_URL = "/api/v1/workspaces/settings/"


def _url(post_id: int, action: str) -> str:
    return f"/api/v1/posts/{post_id}/{action}/"


@pytest.fixture
def chain(advanced_workspace: Any) -> Any:
    return approvals.default_chain(advanced_workspace)


@pytest.fixture
def draft(advanced_workspace: Any, contributor_user: Any) -> Any:
    return create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="New drop"
    )


@pytest.fixture
def second_admin(advanced_workspace: Any) -> Any:
    admin = get_user_model().objects.create_user(email="legal@example.com", password="x")
    Membership.objects.create(user=admin, workspace=advanced_workspace, role=Role.ADMIN)
    return admin


def _stage(chain: Any, *, order: int, name: str, **kwargs: Any) -> ApprovalStage:
    approvers = kwargs.pop("approvers", [])
    stage = ApprovalStage.objects.create(chain=chain, order=order, name=name, **kwargs)
    stage.required_approvers.set(approvers)
    return stage


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
def test_every_workspace_is_born_with_a_chain(workspace: Any) -> None:
    """A workspace with no chain has no defined answer to "who says yes", and
    every caller would have to invent one."""
    chain = workspace.approval_chains.get()

    assert chain.is_default is True
    assert chain.blocks_publish is False


def test_a_workspace_can_hold_only_one_default_chain(workspace: Any) -> None:
    from django.db import IntegrityError, transaction

    from workspaces.models import ApprovalChain

    with pytest.raises(IntegrityError), transaction.atomic():
        ApprovalChain.objects.create(workspace=workspace, name="Second", is_default=True)


def test_the_settings_toggle_writes_the_chain_not_a_column(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    api = client_as(admin_user)

    api.patch(SETTINGS_URL, {"requires_approval": False}, format="json")

    assert approvals.default_chain(advanced_workspace).blocks_publish is False


def test_concurrent_stage_appends_cannot_collide_on_the_same_order(
    advanced_workspace: Any, admin_user: Any
) -> None:
    """Found in review: `order` was a plain `aggregate(Max("order"))` read
    followed by a separate `create()`, with no lock and no atomic wrap — two
    admins configuring stages at the same instant could both compute the same
    next order, and the second `create()` would hit `unique_approval_stage_
    order` as a raw, uncaught `IntegrityError`, which `common.exceptions`'s
    handler does not special-case, so it falls through to a bare 500.

    Real threads against real Postgres, the same shape
    `billing.tests.test_quota_trial::test_concurrent_spends_cannot_exceed_the_
    quota` already uses for the parallel org-ledger race.
    """
    import threading

    from django.db import connections
    from rest_framework.test import APIClient

    outcomes: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def append_stage(name: str) -> None:
        barrier.wait(timeout=10)
        try:
            api = APIClient()
            api.force_authenticate(admin_user)
            response = api.post(CHAIN_URL, {"name": name}, format="json")
            if response.status_code == 201:
                outcomes.append(response.status_code)
            else:
                errors.append(f"{response.status_code}: {response.content!r}")
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=append_stage, args=("Brand lead",)),
        threading.Thread(target=append_stage, args=("Legal",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == [], f"a concurrent append failed instead of queueing: {errors}"
    assert outcomes == [201, 201]

    chain = approvals.default_chain(advanced_workspace)
    assert sorted(chain.stages.values_list("order", flat=True)) == [1, 2]


test_concurrent_stage_appends_cannot_collide_on_the_same_order = pytest.mark.django_db(
    transaction=True
)(test_concurrent_stage_appends_cannot_collide_on_the_same_order)


def test_a_second_stage_is_an_advanced_feature(
    client_as: Any, advanced_workspace: Any, admin_user: Any, plans: Any
) -> None:
    """P2-13. Advanced is re-pitched on **depth**, not on approval existing —
    so the first stage is free everywhere and the second is the sale."""
    advanced_workspace.organization.plan = plans["pro"]
    advanced_workspace.organization.save(update_fields=["plan"])
    api = client_as(admin_user)

    first = api.post(CHAIN_URL, {"name": "Brand lead"}, format="json")
    assert first.status_code == 201

    second = api.post(CHAIN_URL, {"name": "Legal"}, format="json")

    assert second.status_code == 402
    error = second.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]["suggested_plan"] == "advanced"


def test_advanced_may_stack_stages(
    client_as: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    api = client_as(admin_user)

    api.post(CHAIN_URL, {"name": "Brand lead"}, format="json")
    body = api.post(CHAIN_URL, {"name": "Legal"}, format="json").json()

    assert [stage["order"] for stage in body["stages"]] == [1, 2]
    assert [stage["name"] for stage in body["stages"]] == ["Brand lead", "Legal"]


def test_a_stage_with_a_post_waiting_on_it_cannot_be_removed(
    client_as: Any,
    advanced_workspace: Any,
    admin_user: Any,
    chain: Any,
    draft: Any,
    contributor_user: Any,
) -> None:
    """Removing it would leave the post approved by a step that no longer
    exists."""
    _stage(chain, order=1, name="Brand lead")
    approvals.submit_for_review(draft, actor=contributor_user)

    response = client_as(admin_user).delete(CHAIN_URL)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stage_in_use"


def test_an_approver_must_be_a_member_of_this_workspace(
    client_as: Any, advanced_workspace: Any, admin_user: Any, other_user: Any
) -> None:
    response = client_as(admin_user).post(
        CHAIN_URL, {"name": "Legal", "required_approvers": [other_user.pk]}, format="json"
    )

    assert response.status_code == 400


def test_configuring_the_chain_needs_admin(
    client_as: Any, advanced_workspace: Any, contributor_user: Any
) -> None:
    assert (
        client_as(contributor_user).post(CHAIN_URL, {"name": "x"}, format="json").status_code == 403
    )


# -----------------------------------------------------------------------------
# The state machine — one code path, several configurations
# -----------------------------------------------------------------------------
def test_a_two_stage_chain_stays_pending_review_between_stages(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any, second_admin: Any
) -> None:
    """**No new statuses** (P2-07). `PENDING_REVIEW` means "at stage N", and
    `current_stage` says which — putting the chain's shape into `PostStatus`
    would make every new chain shape a migration."""
    brand = _stage(chain, order=1, name="Brand lead")
    legal = _stage(chain, order=2, name="Legal")

    post = approvals.submit_for_review(draft, actor=contributor_user)
    assert post.current_stage_id == brand.pk

    post = approvals.approve(post, actor=admin_user)

    assert post.status == PostStatus.PENDING_REVIEW
    assert post.current_stage_id == legal.pk

    post = approvals.approve(post, actor=second_admin)

    assert post.status == PostStatus.APPROVED
    assert post.current_stage_id is None


def test_min_approvals_holds_the_post_at_the_stage(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any, second_admin: Any
) -> None:
    stage = _stage(chain, order=1, name="Two sign-offs", min_approvals=2)
    post = approvals.submit_for_review(draft, actor=contributor_user)

    post = approvals.approve(post, actor=admin_user)
    assert post.status == PostStatus.PENDING_REVIEW

    post = approvals.approve(post, actor=second_admin)
    assert post.status == PostStatus.APPROVED
    assert ApprovalAction.objects.filter(post=post, stage=stage, action="APPROVE").count() == 2


def test_the_same_person_approving_twice_does_not_clear_a_two_approval_stage(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    """Counted by distinct actor. Otherwise "two sign-offs" is one person
    clicking twice, which is not what anybody bought."""
    _stage(chain, order=1, name="Two sign-offs", min_approvals=2)
    post = approvals.submit_for_review(draft, actor=contributor_user)

    approvals.approve(post, actor=admin_user)
    post.refresh_from_db()
    post = approvals.approve(post, actor=admin_user)

    assert post.status == PostStatus.PENDING_REVIEW


def test_requesting_changes_sends_the_post_back_to_the_first_stage(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any, second_admin: Any
) -> None:
    """The earlier stages approved text that no longer exists."""
    brand = _stage(chain, order=1, name="Brand lead")
    _stage(chain, order=2, name="Legal")

    post = approvals.submit_for_review(draft, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)
    post = approvals.request_changes(post, actor=second_admin, note="Tighten the hook")

    assert post.status == PostStatus.CHANGES_REQUESTED
    assert post.current_stage_id is None

    post = approvals.submit_for_review(post, actor=contributor_user)
    assert post.current_stage_id == brand.pk


# -----------------------------------------------------------------------------
# P2-08 — every emittable code, distinguishable
# -----------------------------------------------------------------------------
def test_naming_a_stage_the_post_has_left_is_409(
    client_as: Any,
    chain: Any,
    draft: Any,
    contributor_user: Any,
    admin_user: Any,
    second_admin: Any,
) -> None:
    """The lost-update guard. A client rendering a three-stage chain posts the
    stage id it drew; by then a colleague may have advanced the post, and
    approving "whatever stage it is now" would approve a stage nobody read."""
    brand = _stage(chain, order=1, name="Brand lead")
    _stage(chain, order=2, name="Legal")
    post = approvals.submit_for_review(draft, actor=contributor_user)
    approvals.approve(post, actor=admin_user)

    response = client_as(second_admin).post(
        _url(post.pk, "approve"), {"stage": brand.pk}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stage_conflict"


def test_lacking_the_approve_permission_is_403(
    client_as: Any, chain: Any, draft: Any, contributor_user: Any
) -> None:
    """A CONTRIBUTOR holds `edit` and not `approve`. 403, never 402: no upgrade
    fixes not holding a permission."""
    _stage(chain, order=1, name="Brand lead")
    approvals.submit_for_review(draft, actor=contributor_user)

    response = client_as(contributor_user).post(_url(draft.pk, "approve"), {}, format="json")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_not_being_on_this_stages_approver_list_is_403(
    client_as: Any,
    chain: Any,
    draft: Any,
    contributor_user: Any,
    admin_user: Any,
    second_admin: Any,
) -> None:
    """Also 403 and also not 402 — but a *different* refusal from the one
    above: this person holds `approve` workspace-wide and is not one of the
    people this stage names."""
    _stage(chain, order=1, name="Legal", approvers=[second_admin])
    approvals.submit_for_review(draft, actor=contributor_user)

    response = client_as(admin_user).post(_url(draft.pk, "approve"), {}, format="json")

    assert response.status_code == 403
    assert "approvers" in response.json()["error"]["message"]


def test_approving_your_own_post_is_403_unless_the_stage_allows_it(
    chain: Any, advanced_workspace: Any, admin_user: Any
) -> None:
    """The author approving their own work is the failure a review stage exists
    to prevent, so allowing it has to be typed."""
    from rest_framework.exceptions import PermissionDenied

    stage = _stage(chain, order=1, name="Brand lead")
    mine = create_post(workspace=advanced_workspace, author=admin_user, master_body="Mine")
    post = approvals.submit_for_review(mine, actor=admin_user)

    with pytest.raises(PermissionDenied):
        approvals.approve(post, actor=admin_user)

    stage.allow_self_approve = True
    stage.save(update_fields=["allow_self_approve"])
    post.refresh_from_db()

    assert approvals.approve(post, actor=admin_user).status == PostStatus.APPROVED


def test_an_illegal_transition_is_409(
    client_as: Any, chain: Any, draft: Any, admin_user: Any
) -> None:
    """Approving a `DRAFT`. Distinct from the stage conflict above: the fix is
    "submit it first", not "reload"."""
    response = client_as(admin_user).post(_url(draft.pk, "approve"), {}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"


# -----------------------------------------------------------------------------
# P2-10 — auto-schedule on final approval, through the one write path
# -----------------------------------------------------------------------------
def test_the_proposed_time_is_scheduled_when_the_last_stage_clears(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any, advanced_social_account: Any
) -> None:
    """The author picks when it should go out; the reviewer says yes; nobody
    comes back to press a second button."""
    _stage(chain, order=1, name="Brand lead")
    when = timezone.now() + dt.timedelta(days=1)

    post = approvals.submit_for_review(
        draft, actor=contributor_user, delivery_mode="AUTO_PUBLISH", scheduled_at=when
    )
    assert post.scheduled_at is None  # a proposal, not a schedule

    post = approvals.approve(post, actor=admin_user)

    assert post.status == PostStatus.SCHEDULED
    assert post.scheduled_at == when
    # Consumed, so a later re-approval cannot silently reschedule it again.
    assert post.proposed_delivery_mode == ""
    assert post.proposed_scheduled_at is None


def test_a_stale_proposal_is_not_silently_scheduled_when_a_slow_chain_finally_clears(
    chain: Any,
    draft: Any,
    contributor_user: Any,
    admin_user: Any,
    second_admin: Any,
    advanced_social_account: Any,
) -> None:
    """Found in review: a multi-stage chain can sit `PENDING_REVIEW` for days.
    `schedule_post` only ever checked an *upper* bound on the proposed time
    (`require_scheduling_horizon`), never that it was still in the future — so
    a stale proposal reaching `_finalise_approval` at the last stage would
    schedule (or, worse, `AUTO_PUBLISH`) at a moment nobody chose, the instant
    the beat scan next ran, because a past `scheduled_at` is already due.

    The fix degrades rather than errors: the approval itself still lands
    (`APPROVED`, locked — the reviewer's decision was legitimate), but a stale
    proposal is left **on the post, untouched**, for a human to re-decide —
    the same shape `scheduler is None` (the guest path) already uses for "not
    scheduled yet, and that is deliberate."
    """
    _stage(chain, order=1, name="Brand lead")
    _stage(chain, order=2, name="Legal")
    when = timezone.now() + dt.timedelta(days=1)

    post = approvals.submit_for_review(
        draft, actor=contributor_user, delivery_mode="AUTO_PUBLISH", scheduled_at=when
    )
    post = approvals.approve(post, actor=admin_user)  # stage 1 clears
    assert post.status == PostStatus.PENDING_REVIEW

    # The second reviewer takes their time — long enough that the proposed
    # slot has now passed. Set directly rather than travelling real time: the
    # scenario is "the clock moved", and the effect is identical either way.
    post.proposed_scheduled_at = timezone.now() - dt.timedelta(hours=1)
    post.save(update_fields=["proposed_scheduled_at"])

    post = approvals.approve(post, actor=second_admin)  # the last stage clears

    assert post.status == PostStatus.APPROVED
    assert post.locked_at is not None  # the approval itself still landed
    assert post.scheduled_at is None  # never silently scheduled at a stale time
    # Left visible rather than discarded, so a human can pick a new time.
    assert post.proposed_delivery_mode == "AUTO_PUBLISH"
    assert post.proposed_scheduled_at is not None
    assert post.proposed_scheduled_at < timezone.now()


def test_a_submission_without_a_proposal_just_lands_approved(
    chain: Any, draft: Any, contributor_user: Any, admin_user: Any
) -> None:
    _stage(chain, order=1, name="Brand lead")

    post = approvals.submit_for_review(draft, actor=contributor_user)
    post = approvals.approve(post, actor=admin_user)

    assert post.status == PostStatus.APPROVED
    assert post.scheduled_at is None


def test_half_a_proposal_is_a_400(
    client_as: Any, advanced_workspace: Any, draft: Any, contributor_user: Any
) -> None:
    """A mode with no time is unschedulable and a time with no mode is
    ambiguous — either would be stored, ignored at approval, and discovered as
    "why didn't it go out"."""
    response = client_as(contributor_user).post(
        _url(draft.pk, "submit"), {"delivery_mode": "AUTO_PUBLISH"}, format="json"
    )

    assert response.status_code == 400
