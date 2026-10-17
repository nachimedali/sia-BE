"""Exactly-once publishing through a background provider (SA-31, SA-32).

The fake runs in SocialAPI mode (`async_publish`): a publish is journalled as
a draft, requested, and answered *pending*; the reconciler reads the outcome.
These tests hold the task-level promises: a pending target is never sent
twice, the outcome lands where the rest of the system reads it, abandoned
drafts and provider media are cleaned up, and an id never crosses vendors.
"""

from __future__ import annotations

import datetime as dt
from io import StringIO
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from channels.adapters.base import PlatformError
from content.models import (
    MediaAsset,
    Post,
    PostStatus,
    PostTarget,
    PostTargetState,
    ProviderState,
)
from content.services.posts import create_post
from scheduling import publishing, reconcile, tasks
from scheduling.journal import TargetJournal, publish_media
from scheduling.tests.test_publishing import _make_due, _scheduled_post

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake(platform_adapter: Any) -> Any:
    platform_adapter.async_publish = True
    return platform_adapter


def _due_post(workspace: Any, user: Any) -> Post:
    return _make_due(_scheduled_post(workspace, user))


def _age(target: PostTarget, **delta: float) -> None:
    PostTarget.objects.filter(pk=target.pk).update(
        provider_state_at=timezone.now() - dt.timedelta(**delta)
    )


# -----------------------------------------------------------------------------
# The publish: journalled, pending, never sent twice
# -----------------------------------------------------------------------------
def test_a_background_publish_leaves_the_target_publishing_with_its_journal(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)

    publishing.publish_post_by_id(post.pk)

    target = PostTarget.objects.get(post=post)
    post.refresh_from_db()
    assert target.state == PostTargetState.PUBLISHING
    assert target.provider_state == ProviderState.PUBLISH_REQUESTED
    assert target.provider_post_id == "fake-draft-1"
    assert target.provider_key == "fake"
    assert target.provider_state_at is not None
    assert post.status == PostStatus.PUBLISHING, "not published until the platform says so"


def test_running_the_publish_again_sends_nothing(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)

    publishing.publish_post_by_id(post.pk)

    assert fake.drafts_created == 1
    assert fake.publish_requests == ["fake-draft-1"]


def test_a_worker_lost_between_draft_and_publish_publishes_that_draft_once(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    fake.crash_after_draft = True

    with pytest.raises(PlatformError):
        publishing.publish_post_by_id(post.pk)
    target = PostTarget.objects.get(post=post)
    assert target.provider_state == ProviderState.DRAFT_CREATED
    assert target.state == PostTargetState.PENDING, "retryable: left for the next attempt"

    publishing.publish_post_by_id(post.pk)

    assert fake.drafts_created == 1
    assert fake.publish_requests == ["fake-draft-1"]


# -----------------------------------------------------------------------------
# The reconciler reads the outcome
# -----------------------------------------------------------------------------
def test_a_confirmed_publish_becomes_published_with_the_platforms_id(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)
    target = PostTarget.objects.get(post=post)
    _age(target, minutes=2)
    fake.settle("fake-draft-1")

    assert reconcile.reconcile_pending() == 1

    target.refresh_from_db()
    post.refresh_from_db()
    assert target.state == PostTargetState.PUBLISHED
    assert target.provider_state == ProviderState.PUBLISHED
    assert target.platform_post_id == "native-fake-draft-1"
    assert target.permalink == "https://fake-platform.test/p/fake-draft-1"
    assert target.published_at is not None, "the capture ladder starts from here"
    assert post.status == PostStatus.PUBLISHED


def test_a_platform_refusal_fails_the_target_and_tells_the_owner(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any, outbox: list[Any]
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)
    target = PostTarget.objects.get(post=post)
    _age(target, minutes=2)
    fake.settle("fake-draft-1", published=False)

    reconcile.reconcile_pending()

    target.refresh_from_db()
    post.refresh_from_db()
    assert target.state == PostTargetState.FAILED
    assert target.provider_state == ProviderState.FAILED
    assert target.error_detail["message"] == "The platform refused the post."
    assert post.status == PostStatus.FAILED
    assert [mail.template for mail in outbox] == ["publish_failed"]


def test_a_fresh_publish_is_left_alone_for_the_grace_period(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)
    fake.settle("fake-draft-1")

    assert reconcile.reconcile_pending() == 0


def test_still_publishing_is_waited_on_then_handed_to_a_person_after_a_day(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)
    target = PostTarget.objects.get(post=post)

    _age(target, hours=3)
    assert reconcile.reconcile_pending() == 0
    target.refresh_from_db()
    assert target.state == PostTargetState.PUBLISHING

    _age(target, hours=25)
    reconcile.reconcile_pending()
    target.refresh_from_db()
    assert target.state == PostTargetState.FAILED
    assert target.error_detail["code"] == "publish_unconfirmed"
    assert fake.publish_requests == ["fake-draft-1"], "never published again"


def test_a_target_from_another_provider_is_not_read(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    publishing.publish_post_by_id(post.pk)
    target = PostTarget.objects.get(post=post)
    PostTarget.objects.filter(pk=target.pk).update(provider_key="zernio")
    _age(target, minutes=5)
    fake.settle("fake-draft-1")

    assert reconcile.reconcile_pending() == 0


# -----------------------------------------------------------------------------
# An id never crosses vendors
# -----------------------------------------------------------------------------
def test_an_account_connected_through_another_provider_is_refused(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    social_account.provider_key = "zernio"
    social_account.save(update_fields=["provider_key"])
    post = _due_post(paid_workspace, user)

    publishing.publish_post_by_id(post.pk)

    target = PostTarget.objects.get(post=post)
    assert target.state == PostTargetState.FAILED
    assert "Reconnect" in target.error_detail["message"]
    assert fake.drafts_created == 0


# -----------------------------------------------------------------------------
# Drafts that will never go out, and provider media
# -----------------------------------------------------------------------------
def _journalled(post: Post, *, state: str, pid: str, target_state: str) -> PostTarget:
    target = PostTarget.objects.get(post=post)
    PostTarget.objects.filter(pk=target.pk).update(
        provider_key="fake",
        provider_state=state,
        provider_post_id=pid,
        provider_state_at=timezone.now() - dt.timedelta(hours=1),
        state=target_state,
    )
    target.refresh_from_db()
    return target


def test_a_failed_targets_draft_is_discarded(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    fake.posts["draft-x"] = "draft"
    target = _journalled(
        post, state=ProviderState.DRAFT_CREATED, pid="draft-x", target_state="FAILED"
    )

    assert reconcile.sweep_drafts() == 1

    target.refresh_from_db()
    assert target.provider_state == ProviderState.DISCARDED
    assert fake.discarded == ["draft-x"]


def test_a_cancelled_posts_draft_is_discarded_once_stale(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    Post.objects.filter(pk=post.pk).update(status=PostStatus.CANCELLED)
    fake.posts["draft-y"] = "draft"
    _journalled(post, state=ProviderState.DRAFT_CREATED, pid="draft-y", target_state="PENDING")

    assert reconcile.sweep_drafts() == 1


def test_a_draft_still_being_retried_is_kept(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    fake.posts["draft-z"] = "draft"
    _journalled(post, state=ProviderState.DRAFT_CREATED, pid="draft-z", target_state="PENDING")

    assert reconcile.sweep_drafts() == 0
    assert fake.discarded == []


def test_provider_media_is_released_after_the_publish_and_in_use_media_kept(
    paid_workspace: Any, user: Any, social_account: Any, fake: Any
) -> None:
    post = _due_post(paid_workspace, user)
    target = _journalled(post, state=ProviderState.PUBLISHED, pid="p-1", target_state="PUBLISHED")
    PostTarget.objects.filter(pk=target.pk).update(provider_media_ids=["m-1", "m-2"])
    fake.media_in_use = {"m-2"}

    assert reconcile.release_media() == 1

    target.refresh_from_db()
    assert target.provider_media_ids == ["m-2"]
    assert fake.released_media == ["m-1"]


def test_orphan_drafts_at_the_vendor_are_swept_and_ours_are_kept(
    paid_workspace: Any, user: Any, monkeypatch: Any
) -> None:
    from social_providers.tests.stub import StubVendor

    vendor = StubVendor()
    orphan = vendor._create({"text": "lost", "targets": [{"account_id": "a"}]}).json()["id"]
    ours = vendor._create({"text": "ours", "targets": [{"account_id": "a"}]}).json()["id"]
    paid_workspace.provider_profile_id = "brand-1"
    paid_workspace.save(update_fields=["provider_profile_id"])
    post = create_post(workspace=paid_workspace, author=user, master_body="x")
    PostTarget.objects.create(post=post, platform="instagram", provider_post_id=ours)
    monkeypatch.setattr(reconcile, "get_platform_adapter", vendor.adapter)

    assert reconcile.sweep_orphan_drafts() == 1
    assert orphan not in vendor.posts
    assert ours in vendor.posts


# -----------------------------------------------------------------------------
# The journal and the media handed to the provider
# -----------------------------------------------------------------------------
def test_each_journal_record_is_on_disk_at_once(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = _due_post(paid_workspace, user)
    target = PostTarget.objects.get(post=post)

    TargetJournal(target).record(
        provider_post_id="p-9", provider_state=ProviderState.DRAFT_CREATED, media_ids=["m-1"]
    )

    stored = PostTarget.objects.get(pk=target.pk)
    assert (stored.provider_post_id, stored.provider_state, stored.provider_media_ids) == (
        "p-9",
        ProviderState.DRAFT_CREATED,
        ["m-1"],
    )


def _asset(workspace: Any, name: str) -> MediaAsset:
    asset = MediaAsset(workspace=workspace, kind="IMAGE", mime="image/jpeg")
    asset.file.save(name, ContentFile(b"\xff\xd8 bytes"), save=True)
    return asset


def test_publish_media_keeps_the_payloads_order_with_absolute_urls_and_bytes(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    first, second = _asset(paid_workspace, "a.jpg"), _asset(paid_workspace, "b.jpg")
    post = _due_post(paid_workspace, user)
    target = PostTarget.objects.get(post=post)
    target.rendered_payload = {
        "media": [
            {"id": second.pk, "kind": "IMAGE", "url": "", "alt": "Second"},
            {"id": first.pk, "kind": "IMAGE", "url": "", "alt": ""},
        ]
    }

    media = publish_media(target)

    assert [item.id for item in media] == [second.pk, first.pk]
    assert media[0].url.startswith(("http://", "https://"))
    assert media[0].size == len(b"\xff\xd8 bytes")
    with media[0].open() as handle:
        assert handle.read() == b"\xff\xd8 bytes"


def test_another_workspaces_media_is_never_handed_over(
    paid_workspace: Any, user: Any, social_account: Any, other_user: Any
) -> None:
    from workspaces.services.provisioning import provision_workspace

    theirs = _asset(provision_workspace(other_user, name="Not Yours"), "theirs.jpg")
    post = _due_post(paid_workspace, user)
    target = PostTarget.objects.get(post=post)
    target.rendered_payload = {"media": [{"id": theirs.pk, "kind": "IMAGE", "url": "", "alt": ""}]}

    with pytest.raises(PlatformError) as caught:
        publish_media(target)

    assert caught.value.code == "publish_media_missing"


# -----------------------------------------------------------------------------
# Scheduled and runnable by hand
# -----------------------------------------------------------------------------
def test_the_reconciler_is_scheduled_and_runs_from_the_command(fake: Any) -> None:
    from django.conf import settings

    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    assert "scheduling.tasks.publish_reconcile" in scheduled
    assert "scheduling.tasks.publish_sweep_orphan_drafts" in scheduled
    assert tasks.publish_reconcile() == {"settled": 0, "drafts_discarded": 0, "media_released": 0}

    out = StringIO()
    call_command("reconcile_publishing", "--orphans", stdout=out)
    assert "settled: 0" in out.getvalue()
