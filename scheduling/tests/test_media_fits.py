"""An auto-publish goes out in a content type its media makes, or not at all.

Found on beta 2026-11-14: three Instagram videos scheduled as a "Post" (one
image only) reached the provider with their video stripped and were refused
("Instagram posts require at least one image or video"). The content type now
follows the media, and what no content type can carry is refused when it is
scheduled, where it can still be fixed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from channels.models import SocialAccount
from content.models import MediaAsset, PostFormat, PostStatus, PostTarget
from content.services.posts import _write_format, create_post

pytestmark = pytest.mark.django_db


def _schedule(client: Any, post_id: int, mode: str = "AUTO_PUBLISH") -> Any:
    return client.post(
        f"/api/v1/posts/{post_id}/schedule/",
        {
            "delivery_mode": mode,
            "scheduled_at": (timezone.now() + dt.timedelta(days=1)).isoformat(),
        },
        format="json",
    )


def _video(workspace: Any) -> MediaAsset:
    return MediaAsset.objects.create(
        workspace=workspace, kind="VIDEO", mime="video/mp4", duration_ms=5000
    )


def test_a_video_left_on_post_goes_to_instagram_as_a_reel(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = create_post(
        workspace=paid_workspace,
        author=user,
        master_body="New drop",
        media_assets=[_video(paid_workspace)],
    )
    _write_format(post, "instagram", PostFormat.FEED)

    response = _schedule(auth_client, post.id)

    assert response.status_code == 200, response.json()
    target = PostTarget.objects.get(post=post, social_account=social_account)
    assert target.post_format == PostFormat.REEL
    assert len(target.rendered_payload["media"]) == 1


def test_media_no_content_type_takes_is_refused_per_platform(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="pinterest",
        handle="@acme",
        display_name="Acme",
        provider_account_id="acct-pinterest-1",
    )
    post = create_post(
        workspace=paid_workspace,
        author=user,
        master_body="New drop",
        media_assets=[_video(paid_workspace)],
    )

    response = _schedule(auth_client, post.id)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "media_does_not_fit"
    # Instagram makes a reel of it; only Pinterest, image-only, is named.
    assert set(error["detail"]) == {"pinterest"}
    assert "Pinterest" in error["detail"]["pinterest"]["post_format"]
    post.refresh_from_db()
    assert post.status == PostStatus.DRAFT
    assert not PostTarget.objects.filter(post=post, social_account__isnull=False).exists()


def test_instagram_with_no_media_is_refused(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    post = create_post(workspace=paid_workspace, author=user, master_body="Text only")

    response = _schedule(auth_client, post.id)

    assert response.status_code == 400
    assert set(response.json()["error"]["detail"]) == {"instagram"}


def test_a_platform_turned_off_is_not_checked(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    SocialAccount.objects.create(
        workspace=paid_workspace,
        platform="linkedin",
        handle="@acme",
        display_name="Acme",
        provider_account_id="acct-linkedin-1",
    )
    post = create_post(workspace=paid_workspace, author=user, master_body="Text only")
    post.planned_platforms = ["linkedin"]
    post.save(update_fields=["planned_platforms"])

    assert _schedule(auth_client, post.id).status_code == 200


def test_a_reminder_is_not_refused_for_its_media(
    auth_client: Any, paid_workspace: Any, user: Any, social_account: Any
) -> None:
    # A reminder is posted by hand in the platform's own app, which takes it.
    post = create_post(workspace=paid_workspace, author=user, master_body="Text only")

    assert _schedule(auth_client, post.id, mode="REMINDER").status_code == 200
