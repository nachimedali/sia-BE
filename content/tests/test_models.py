"""Model-level behaviour (design.md §6.3/§6.4)."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import IntegrityError, transaction

from content.models import MediaKind, Platform, PostTarget, PostTargetState
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


def test_ordered_media_follows_attachment_order(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    from content.services.media import ingest_media

    second = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))
    third = ingest_media(workspace=workspace, upload=make_png_upload("c.png"))

    post = create_post(
        workspace=workspace,
        author=user,
        master_body="carousel",
        media_assets=[third, media_asset, second],
    )

    assert list(post.ordered_media()) == [third, media_asset, second]


def test_replacing_media_drops_the_old_attachments(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    from content.services.media import ingest_media
    from content.services.posts import update_post

    other = ingest_media(workspace=workspace, upload=make_png_upload("other.png"))
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])

    update_post(post, media_asset_ids=[other])

    assert list(post.ordered_media()) == [other]


def test_media_asset_aspect_ratio(workspace: Any, media_asset: Any) -> None:
    assert media_asset.kind == MediaKind.IMAGE
    assert media_asset.aspect_ratio == pytest.approx(1.0)


def test_post_target_unique_per_post_and_platform(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="hi")
    PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)

    with pytest.raises(IntegrityError), transaction.atomic():
        PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)


def test_post_target_defaults_to_pending(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user)
    target = PostTarget.objects.create(post=post, platform=Platform.TIKTOK)

    assert target.state == PostTargetState.PENDING
    assert target.rendered_payload == {}
    assert target.idempotency_key is None


def test_post_target_idempotency_key_allows_many_nulls(workspace: Any, user: Any) -> None:
    """NULL, not empty string — Postgres does not treat repeated NULLs as
    duplicates under a unique constraint, which is what lets every target
    created before Phase 6 populates keys coexist."""
    post = create_post(workspace=workspace, author=user)
    PostTarget.objects.create(post=post, platform=Platform.INSTAGRAM)
    PostTarget.objects.create(post=post, platform=Platform.TIKTOK)
