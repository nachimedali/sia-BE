"""Composer metadata, from the form to the provider (P1-11).

`platform_options` has existed since P1-03 and been validated since P1-05 —
and until now nothing read it. **An option that never reaches the provider is
decoration**, so this file's centre of gravity is the path: a value set on a
target must appear in the rendered payload, and the rendered payload is what
publish sends.

Two properties are worth naming separately:

* **Resolution happens in `render_post` and nowhere else** (P1-04). Options
  join the body and the media as a third thing a target can override, and a
  third thing is a third opportunity for preview and publish to disagree.
* **A media-referencing option is a tenancy surface.** `thumbnail_media_id` is
  an integer, and an integer validator will happily accept another workspace's
  asset id. That is the kind of hole a "declare it as data" schema opens if the
  vocabulary has no way to say "this one is a reference".
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from content.models import Platform, PostTarget
from content.services import options as option_rules
from content.services.adaptation import render_post
from content.services.media import ingest_media
from content.services.posts import create_post
from content.services.rules import PLATFORM_RULES, options_for
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

OPTIONS_URL = "/api/v1/posts/{pk}/platform-options/"


# -----------------------------------------------------------------------------
# Options reach the payload
# -----------------------------------------------------------------------------
def test_options_reach_the_rendered_payload(workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    PostTarget.objects.create(
        post=post,
        platform=Platform.INSTAGRAM,
        platform_options={"first_comment": "More in the comments 👇"},
    )

    payload = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    assert payload["options"]["first_comment"] == "More in the comments 👇"


def test_declared_defaults_are_filled_in(workspace: Any, user: Any) -> None:
    """A consumer downstream reads one shape and none of them has to remember
    what the default was."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    PostTarget.objects.create(post=post, platform=Platform.TIKTOK, platform_options={})

    payload = render_post(post, [Platform.TIKTOK])[Platform.TIKTOK].as_dict()
    assert payload["options"] == {"allow_comments": True, "allow_duet": True}


def test_a_platform_with_no_target_renders_its_defaults(workspace: Any, user: Any) -> None:
    """Preview asks for every platform at once, including ones with no target
    yet. Those must render the defaults, not an empty object that a composer
    would show as "comments off"."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")

    payload = render_post(post, [Platform.TIKTOK])[Platform.TIKTOK].as_dict()
    assert payload["options"] == {"allow_comments": True, "allow_duet": True}


def test_a_platform_with_nothing_to_configure_renders_an_empty_object(
    workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    payload = render_post(post, [Platform.THREADS])[Platform.THREADS].as_dict()
    assert payload["options"] == {}


def test_a_stored_option_that_is_no_longer_declared_is_dropped(workspace: Any, user: Any) -> None:
    """Rules change; stored rows do not. A key that no longer exists is
    dropped at render rather than passed to a provider that will reject the
    whole post over it."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    PostTarget.objects.create(
        post=post,
        platform=Platform.TIKTOK,
        platform_options={"allow_comments": False, "retired_option": "x"},
    )

    payload = render_post(post, [Platform.TIKTOK])[Platform.TIKTOK].as_dict()
    assert "retired_option" not in payload["options"]
    assert payload["options"]["allow_comments"] is False


def test_options_are_resolved_only_in_render_post() -> None:
    """P1-04's rule, extended to the third overridable thing. A second
    resolver is how preview and publish start disagreeing."""
    import pathlib

    from scheduling import publishing

    source = pathlib.Path(publishing.__file__ or "").read_text()
    assert "platform_options" not in source, (
        "The publish path is resolving options itself. render_post is the only "
        "place that may (P1-04)."
    )


# -----------------------------------------------------------------------------
# The declarations BUILD-PLAN Phase 1 names
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("platform", "key"),
    [
        (Platform.INSTAGRAM, "first_comment"),
        (Platform.INSTAGRAM, "location_id"),
        (Platform.INSTAGRAM, "collab_handles"),
        (Platform.INSTAGRAM, "tagged_handles"),
        (Platform.FACEBOOK, "first_comment"),
        (Platform.FACEBOOK, "location_id"),
        (Platform.FACEBOOK, "targeting_countries"),
        (Platform.FACEBOOK, "targeting_min_age"),
        (Platform.FACEBOOK, "targeting_interests"),
        (Platform.FACEBOOK, "targeting_locales"),
        (Platform.FACEBOOK, "tagged_page_ids"),
        (Platform.LINKEDIN, "first_comment"),
        (Platform.LINKEDIN, "targeting_locales"),
        (Platform.LINKEDIN, "tagged_organization_ids"),
        (Platform.YOUTUBE, "title"),
        (Platform.YOUTUBE, "thumbnail_media_id"),
    ],
)
def test_the_composer_metadata_phase_1_names_is_declared(platform: str, key: str) -> None:
    assert key in options_for(platform)


def test_every_media_option_declares_itself_as_one() -> None:
    """The tenancy guard depends on the *kind*, not on the key's name. An
    option that references media and calls itself an `int` is validated as an
    integer and reaches another tenant's asset."""
    for platform, rule in PLATFORM_RULES.items():
        for option in rule.options:
            if option.key.endswith("_media_id"):
                assert option.kind == "media", f"{platform}.{option.key} must be kind 'media'"


# -----------------------------------------------------------------------------
# A media reference is a tenancy surface
# -----------------------------------------------------------------------------
def test_a_media_option_accepts_the_workspaces_own_asset(workspace: Any, media_asset: Any) -> None:
    cleaned = option_rules.validate(
        Platform.YOUTUBE,
        {"title": "A video", "thumbnail_media_id": media_asset.pk},
        workspace=workspace,
    )
    assert cleaned["thumbnail_media_id"] == media_asset.pk


def test_a_media_option_refuses_another_workspaces_asset(
    workspace: Any, make_png_upload: Any
) -> None:
    """An integer validator would wave this through. The point of a `media`
    kind is that it cannot."""
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))

    with pytest.raises(option_rules.OptionError) as caught:
        option_rules.validate(
            Platform.YOUTUBE,
            {"title": "A video", "thumbnail_media_id": foreign.pk},
            workspace=workspace,
        )
    assert "thumbnail_media_id" in caught.value.errors


def test_a_media_option_is_refused_when_no_workspace_is_known(workspace: Any) -> None:
    """Fail closed. A caller that cannot say which workspace is asking must not
    get a pass on the check that needs to know."""
    with pytest.raises(option_rules.OptionError):
        option_rules.validate(Platform.YOUTUBE, {"title": "A video", "thumbnail_media_id": 1})


# -----------------------------------------------------------------------------
# The endpoint
# -----------------------------------------------------------------------------
def test_setting_options_over_the_api(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")

    response = auth_client.post(
        OPTIONS_URL.format(pk=post.pk),
        {"platform": Platform.INSTAGRAM, "options": {"first_comment": "Swipe →"}},
        format="json",
    )
    assert response.status_code == 200

    target = post.targets.get(platform=Platform.INSTAGRAM)
    assert target.platform_options["first_comment"] == "Swipe →"
    assert target.options_schema_version > 0


def test_setting_options_creates_the_target_if_absent(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    """A composer configures a platform before the post is scheduled, which is
    before `build_targets` has run."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    assert not post.targets.exists()

    auth_client.post(
        OPTIONS_URL.format(pk=post.pk),
        {"platform": Platform.LINKEDIN, "options": {"visibility": "CONNECTIONS"}},
        format="json",
    )
    assert post.targets.get(platform=Platform.LINKEDIN).platform_options["visibility"] == (
        "CONNECTIONS"
    )


def test_an_undeclared_option_is_400_naming_the_field(
    auth_client: Any, workspace: Any, user: Any
) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    response = auth_client.post(
        OPTIONS_URL.format(pk=post.pk),
        {"platform": Platform.INSTAGRAM, "options": {"nope": 1}},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_platform_options"
    assert "nope" in response.json()["error"]["detail"]


def test_a_missing_required_option_is_400(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    response = auth_client.post(
        OPTIONS_URL.format(pk=post.pk),
        {"platform": Platform.YOUTUBE, "options": {}},
        format="json",
    )
    assert response.status_code == 400
    assert "title" in response.json()["error"]["detail"]


def test_setting_options_records_a_revision(auth_client: Any, workspace: Any, user: Any) -> None:
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    auth_client.post(
        OPTIONS_URL.format(pk=post.pk),
        {"platform": Platform.LINKEDIN, "options": {"visibility": "CONNECTIONS"}},
        format="json",
    )
    assert post.revisions.count() == 2


def test_another_workspaces_post_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = create_post(workspace=theirs, author=stranger, master_body="not yours")

    response = auth_client.post(
        OPTIONS_URL.format(pk=foreign.pk),
        {"platform": Platform.LINKEDIN, "options": {}},
        format="json",
    )
    assert response.status_code == 404


def test_another_organizations_post_is_404(auth_client: Any, workspace: Any) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign = create_post(workspace=theirs, author=stranger, master_body="not yours")
    response = auth_client.post(
        OPTIONS_URL.format(pk=foreign.pk),
        {"platform": Platform.LINKEDIN, "options": {}},
        format="json",
    )
    assert response.status_code == 404


# -----------------------------------------------------------------------------
# Carousel order
# -----------------------------------------------------------------------------
def test_reordering_a_carousel_changes_what_publish_sends(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    from content.services.posts import update_post

    second = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset, second])

    before = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    assert [item["id"] for item in before["media"]] == [media_asset.pk, second.pk]

    update_post(post, author=user, media_asset_ids=[second, media_asset])

    after = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    assert [item["id"] for item in after["media"]] == [second.pk, media_asset.pk]


def test_a_media_option_renders_as_a_url(workspace: Any, user: Any, media_asset: Any) -> None:
    """A provider cannot fetch a row from our database. The id is what is
    stored and checked; the URL is what is sent — the same split
    `AdaptedMedia` already makes."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    PostTarget.objects.create(
        post=post,
        platform=Platform.YOUTUBE,
        platform_options={"title": "A video", "thumbnail_media_id": media_asset.pk},
    )

    payload = render_post(post, [Platform.YOUTUBE])[Platform.YOUTUBE].as_dict()
    assert payload["options"]["thumbnail_media_id"] == media_asset.file.url


def test_a_thumbnail_whose_file_is_gone_drops_out(workspace: Any, user: Any) -> None:
    """Dropping the setting is right; sending a dangling id is not."""
    post = create_post(workspace=workspace, author=user, master_body="Hello")
    PostTarget.objects.create(
        post=post,
        platform=Platform.YOUTUBE,
        platform_options={"title": "A video", "thumbnail_media_id": 999999},
    )

    payload = render_post(post, [Platform.YOUTUBE])[Platform.YOUTUBE].as_dict()
    assert "thumbnail_media_id" not in payload["options"]
    assert payload["options"]["title"] == "A video"
