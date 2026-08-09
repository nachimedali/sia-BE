"""Content endpoints (design.md §7)."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from config.api_urls import router
from content.services.adaptation import render_post
from content.services.media import ingest_media
from content.services.posts import create_post
from products.services.products import create_product
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

POSTS_URL = "/api/v1/posts/"
PREVIEW_URL = "/api/v1/posts/preview/"
MEDIA_URL = "/api/v1/media/"


# -----------------------------------------------------------------------------
# Post CRUD
# -----------------------------------------------------------------------------
def test_create_post_assigns_workspace_author_and_defaults(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    response = auth_client.post(POSTS_URL, {"master_body": "Hello workspace"}, format="json")
    assert response.status_code == 201

    body = response.json()
    assert body["master_body"] == "Hello workspace"
    assert body["status"] == "DRAFT"
    assert body["source"] == "MANUAL"
    assert body["media"] == []


def test_create_post_attaches_media_in_the_order_given(
    auth_client: Any, workspace: Any, media_asset: Any, make_png_upload: Any
) -> None:
    second = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))

    response = auth_client.post(
        POSTS_URL,
        {"master_body": "carousel", "media_asset_ids": [second.id, media_asset.id]},
        format="json",
    )
    assert response.status_code == 201
    assert [m["id"] for m in response.json()["media"]] == [second.id, media_asset.id]


def test_post_list_does_not_n_plus_one_media(
    auth_client: Any,
    workspace: Any,
    user: Any,
    media_asset: Any,
    django_assert_max_num_queries: Any,
) -> None:
    """The list queryset prefetches `media_attachments`, and `ordered_media()`
    has to actually read that prefetch rather than issuing its own query per
    post, or this grows linearly with the number of posts returned."""
    for _ in range(5):
        create_post(workspace=workspace, author=user, master_body="x", media_assets=[media_asset])

    # Auth, count, the page of posts, and one prefetch for their attachments —
    # flat regardless of how many posts are on the page.
    with django_assert_max_num_queries(4):
        response = auth_client.get(POSTS_URL)
    assert response.status_code == 200
    assert len(response.json()["results"]) == 5


def test_status_delivery_mode_and_scheduled_at_are_not_client_writable(
    auth_client: Any, workspace: Any
) -> None:
    """design.md A49 — those fields belong to the Phase 8 schedule endpoint."""
    response = auth_client.post(
        POSTS_URL,
        {"master_body": "hi", "status": "PUBLISHED", "delivery_mode": "AUTO_PUBLISH"},
        format="json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["delivery_mode"] == ""


def test_patch_updates_body_without_touching_media(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(
        workspace=workspace, author=user, master_body="v1", media_assets=[media_asset]
    )

    response = auth_client.patch(f"{POSTS_URL}{post.id}/", {"master_body": "v2"}, format="json")
    assert response.status_code == 200
    body = response.json()
    assert body["master_body"] == "v2"
    assert [m["id"] for m in body["media"]] == [media_asset.id]


def test_media_asset_ids_must_belong_to_the_caller_workspace(
    auth_client: Any, workspace: Any, plans: Any, make_png_upload: Any
) -> None:
    other_owner = get_user_model().objects.create_user(email="other@example.com", password="x")
    other_workspace = provision_workspace(other_owner, name="Someone Else")
    foreign_asset = ingest_media(workspace=other_workspace, upload=make_png_upload())

    response = auth_client.post(
        POSTS_URL, {"master_body": "hi", "media_asset_ids": [foreign_asset.id]}, format="json"
    )
    assert response.status_code == 400


# -----------------------------------------------------------------------------
# Media upload
# -----------------------------------------------------------------------------
def test_media_upload_returns_the_ingested_asset(
    auth_client: Any, workspace: Any, make_png_upload: Any
) -> None:
    response = auth_client.post(MEDIA_URL, {"file": make_png_upload()}, format="multipart")
    assert response.status_code == 201

    body = response.json()
    assert body["kind"] == "IMAGE"
    assert body["width"] == 600
    assert body["url"]


def test_media_upload_without_a_file_is_rejected(auth_client: Any, workspace: Any) -> None:
    response = auth_client.post(MEDIA_URL, {}, format="multipart")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_file"


# -----------------------------------------------------------------------------
# Preview — design.md §8.6
# -----------------------------------------------------------------------------
def test_preview_returns_a_payload_per_requested_platform(
    auth_client: Any, workspace: Any, media_asset: Any
) -> None:
    response = auth_client.post(
        PREVIEW_URL,
        {
            "master_body": "Check out our new drop #launch",
            "media_asset_ids": [media_asset.id],
            "platforms": ["instagram", "linkedin"],
        },
        format="json",
    )
    assert response.status_code == 200
    payloads = response.json()["payloads"]
    assert set(payloads) == {"instagram", "linkedin"}
    assert payloads["instagram"]["hashtags"] == ["launch"]


def test_preview_defaults_to_the_workspace_platforms(auth_client: Any, workspace: Any) -> None:
    workspace.platforms = ["tiktok", "youtube"]
    workspace.save(update_fields=["platforms"])

    response = auth_client.post(PREVIEW_URL, {"master_body": "hi"}, format="json")
    assert response.status_code == 200
    assert set(response.json()["payloads"]) == {"tiktok", "youtube"}


def test_preview_with_no_target_platforms_is_a_client_error(
    auth_client: Any, workspace: Any
) -> None:
    workspace.platforms = []
    workspace.save(update_fields=["platforms"])

    response = auth_client.post(PREVIEW_URL, {"master_body": "hi"}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_target_platforms"


def test_preview_payload_identical_to_publish_payload(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    """design.md §8.6: '...the preview output must be byte-identical to what
    publish sends — same function, one call path.' `render_post` is that
    function; the Phase 9 publish task will call it on the saved post exactly
    as this test does to prove the two can never drift.
    """
    post = create_post(
        workspace=workspace,
        author=user,
        master_body="New drop is live #launch #newcollection",
        media_assets=[media_asset],
    )

    response = auth_client.post(
        PREVIEW_URL,
        {
            "master_body": post.master_body,
            "media_asset_ids": [media_asset.id],
            "platforms": ["instagram"],
        },
        format="json",
    )
    assert response.status_code == 200
    preview_payload = response.json()["payloads"]["instagram"]

    published_payload = render_post(post, ["instagram"])["instagram"].as_dict()

    assert preview_payload == published_payload


# -----------------------------------------------------------------------------
# Tenancy sweep — design.md §11, A52
# -----------------------------------------------------------------------------
def test_router_has_exactly_the_viewsets_this_sweep_covers() -> None:
    """Fails loudly if a ViewSet is registered without updating the count
    below, rather than letting it silently escape the sweep."""
    assert len(router.registry) == 3


def test_cross_workspace_access_returns_404_on_every_viewset(
    auth_client: Any, workspace: Any, user: Any, plans: Any, make_png_upload: Any
) -> None:
    other_owner = get_user_model().objects.create_user(
        email="other-owner@example.com", password="x"
    )
    other_workspace = provision_workspace(other_owner, name="Someone Else's Workspace")

    # Every object below belongs to `other_workspace`, never to `workspace` —
    # the workspace `auth_client` is authenticated into. Each registered
    # ViewSet must 404 on it, not merely decline it.
    post = create_post(workspace=other_workspace, author=other_owner, master_body="not yours")
    media_asset = ingest_media(workspace=other_workspace, upload=make_png_upload())
    product = create_product(workspace=other_workspace, name="Not yours either")

    objects_by_basename = {"post": post, "media-asset": media_asset, "product": product}

    for prefix, _viewset, basename in router.registry:
        obj = objects_by_basename[basename]
        response = auth_client.get(f"/api/v1/{prefix}/{obj.pk}/")
        assert response.status_code == 404, f"{basename} leaked a cross-workspace object"
