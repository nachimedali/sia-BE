"""The approval lock (P2-11).

**Checked at the service layer, not in the view.** Every test below calls the
service directly, because that is where the guarantee has to live: a check in
the ViewSet protects the endpoints that exist today and nothing else — not a
Celery task, not a management command, not next phase's endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import PostStatus
from content.services import revisions
from content.services.posts import create_post, set_alt_text, set_platform_options, update_post
from workspaces.models import AuditLog
from workspaces.services import approvals

pytestmark = pytest.mark.django_db


@pytest.fixture
def approved(advanced_workspace: Any, contributor_user: Any, admin_user: Any) -> Any:
    post = create_post(
        workspace=advanced_workspace, author=contributor_user, master_body="Signed off"
    )
    post = approvals.submit_for_review(post, actor=contributor_user)
    return approvals.approve(post, actor=admin_user)


def test_approval_locks_the_post(approved: Any) -> None:
    assert approved.locked_at is not None


def test_a_locked_post_refuses_a_body_edit(approved: Any) -> None:
    with pytest.raises(approvals.PostLockedError) as caught:
        update_post(approved, master_body="Something else entirely")

    assert caught.value.status_code == 409
    assert caught.value.code == "post_locked"
    approved.refresh_from_db()
    assert approved.master_body == "Signed off"


def test_a_locked_post_refuses_alt_text_and_platform_options(
    approved: Any, media_asset: Any
) -> None:
    """Every mutating service, not only the obvious one. Alt text is exempt
    from *voiding an approval* — describing an image does not change what was
    signed off — but it is not exempt from the lock, because the lock is about
    the post being frozen, not about what counts as content."""
    with pytest.raises(approvals.PostLockedError):
        set_alt_text(approved, media_asset=media_asset, alt_text="A mug")

    with pytest.raises(approvals.PostLockedError):
        set_platform_options(approved, platform="instagram", options={})


def test_a_locked_post_refuses_a_revision_restore(approved: Any, admin_user: Any) -> None:
    with pytest.raises(approvals.PostLockedError):
        revisions.restore(approved, sequence=1, author=admin_user)


def test_history_stays_readable_while_locked(approved: Any) -> None:
    """Locking people out of what a post used to say helps nobody."""
    assert approved.revisions.count() >= 1
    assert revisions.state_at(approved, 1)["master_body"] == "Signed off"


def test_unlocking_is_audited(approved: Any, admin_user: Any, advanced_workspace: Any) -> None:
    """ "Who reopened this" is exactly the question asked after something wrong
    goes out."""
    post = approvals.unlock(approved, actor=admin_user)

    assert post.locked_at is None
    entry = AuditLog.objects.get(workspace=advanced_workspace, verb="post.unlocked")
    assert entry.actor_id == admin_user.pk
    assert entry.meta["post"] == post.pk


def test_unlocking_keeps_the_approval_until_the_content_changes(
    approved: Any, admin_user: Any
) -> None:
    """Unlocking is not a rejection. The *edit* is what voids the approval, and
    that rule predates the lock."""
    post = approvals.unlock(approved, actor=admin_user)
    assert post.status == PostStatus.APPROVED

    post = update_post(post, master_body="Reworded")

    assert post.status == PostStatus.PENDING_REVIEW


def test_unlocking_needs_admin(
    client_as: Any, approved: Any, contributor_user: Any, admin_user: Any
) -> None:
    url = f"/api/v1/posts/{approved.pk}/unlock/"

    assert client_as(contributor_user).post(url).status_code == 403
    assert client_as(admin_user).post(url).status_code == 200


def test_an_open_chain_approval_does_not_lock(workspace: Any, user: Any) -> None:
    """A solo user's post is approved by the act of scheduling it (L-2); there
    is no separate sign-off standing between them and their own draft, so there
    is nothing for a lock to protect."""
    import datetime as dt

    from django.utils import timezone

    from scheduling.services import schedule_post

    post = schedule_post(
        post=create_post(workspace=workspace, author=user, master_body="Mine"),
        delivery_mode="REMINDER",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        actor=user,
    )

    assert post.locked_at is None
