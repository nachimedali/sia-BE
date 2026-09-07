"""Alt text, and where it is allowed to live (P1-06, P1-07).

**Alt text is not a property of the file.** It is a property of *this use of
the file in this post* — the same photograph is "our founder at the 2019
launch" in one post and "the espresso machine we still use" in another, and a
single field on `MediaAsset` would force one of those onto the other. Worse,
`MediaAsset` is immutable by design (its bytes, checksum and probed dimensions
are established once at ingest); giving it an editable text column would put a
mutable field on an immutable row and invite the next one.

So it lives on the post↔media join, and is overridable per target — Instagram
and LinkedIn describe the same image to different audiences.

Written before the columns exist, which is the point: the trap here is a
one-line "just add alt_text to MediaAsset", and a test that names the trap is
cheaper than the migration that undoes it.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from content.models import MediaAsset, Platform, Post, PostMediaAttachment, PostTarget
from content.serializers import MediaAssetSerializer
from content.services.adaptation import render_post
from content.services.media import ingest_media
from content.services.posts import create_post, set_alt_text
from content.views import MediaAssetViewSet
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

ALT_URL = "/api/v1/posts/{pk}/alt-text/"

#: Field names that describe *content* rather than the file. Any of these on
#: `MediaAsset` is the P1-07 mistake, whatever it is called.
DESCRIPTIVE_FIELD_NAMES = frozenset(
    {"alt_text", "alt", "caption", "description", "title", "label", "note", "notes"}
)


# -----------------------------------------------------------------------------
# P1-07 — MediaAsset stays immutable
# -----------------------------------------------------------------------------
def test_media_asset_declares_no_mutable_text_field() -> None:
    """The named trap. `mime` and `checksum` are text, but they are *probed
    from the bytes* at ingest and never edited; a descriptive field is the one
    a user would expect to change, and changing it is what breaks
    immutability."""
    offenders = sorted(
        field.name
        for field in MediaAsset._meta.get_fields()
        if field.name in DESCRIPTIVE_FIELD_NAMES
    )
    assert offenders == [], (
        f"MediaAsset carries descriptive text: {', '.join(offenders)}. "
        "Alt text belongs on PostMediaAttachment — it is a property of this "
        "use of the file, not of the file (P1-06)."
    )


def test_the_media_asset_api_exposes_nothing_writable() -> None:
    """The other half of immutability: a read-only model with a writable
    serializer is mutable in practice."""
    writable = sorted(
        name for name, field in MediaAssetSerializer().fields.items() if not field.read_only
    )
    assert writable == []
    assert not hasattr(MediaAssetViewSet, "update")
    assert not hasattr(MediaAssetViewSet, "partial_update")


# -----------------------------------------------------------------------------
# P1-06 — alt text on the join
# -----------------------------------------------------------------------------
def test_alt_text_is_per_post_not_per_asset(workspace: Any, user: Any, media_asset: Any) -> None:
    """The whole reason it is on the join: one asset, two posts, two truths."""
    launch = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    machine = create_post(workspace=workspace, author=user, media_assets=[media_asset])

    set_alt_text(launch, media_asset=media_asset, alt_text="Our founder at the 2019 launch")
    set_alt_text(machine, media_asset=media_asset, alt_text="The espresso machine we still use")

    assert launch.media_attachments.get().alt_text == "Our founder at the 2019 launch"
    assert machine.media_attachments.get().alt_text == "The espresso machine we still use"


def test_alt_text_reaches_the_rendered_payload(workspace: Any, user: Any, media_asset: Any) -> None:
    """Alt text nobody sends is decoration. This is the assertion that makes
    it a feature."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    set_alt_text(post, media_asset=media_asset, alt_text="A blue ceramic mug on oak")

    payload = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    assert payload["media"][0]["alt"] == "A blue ceramic mug on oak"


def test_alt_text_defaults_to_empty_never_null(workspace: Any, user: Any, media_asset: Any) -> None:
    """An image with no alt text renders `""`, not a missing key and not
    `null` — a consumer that has to distinguish three states for a string is
    a consumer that will get one of them wrong."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    payload = render_post(post, [Platform.INSTAGRAM])[Platform.INSTAGRAM].as_dict()
    assert payload["media"][0]["alt"] == ""


def test_a_target_may_override_the_alt_text(workspace: Any, user: Any, media_asset: Any) -> None:
    """Instagram and LinkedIn describe the same image to different
    audiences."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    linkedin = PostTarget.objects.create(post=post, platform=Platform.LINKEDIN)

    set_alt_text(post, media_asset=media_asset, alt_text="A blue mug")
    set_alt_text(
        post,
        media_asset=media_asset,
        alt_text="Product photography from our Q3 catalogue shoot",
        target=linkedin,
    )

    rendered = render_post(post, [Platform.INSTAGRAM, Platform.LINKEDIN])
    assert rendered[Platform.INSTAGRAM].as_dict()["media"][0]["alt"] == "A blue mug"
    assert (
        rendered[Platform.LINKEDIN].as_dict()["media"][0]["alt"]
        == "Product photography from our Q3 catalogue shoot"
    )


def test_an_override_row_does_not_duplicate_the_media(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    """The trap this join creates: override rows share the table with base
    rows, so anything reading `media_attachments` unfiltered sees the image
    twice and publishes a two-slide carousel of one photograph."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    target = PostTarget.objects.create(post=post, platform=Platform.THREADS)
    set_alt_text(post, media_asset=media_asset, alt_text="Override", target=target)

    assert post.media_attachments.count() == 2  # one base, one override
    assert len(post.ordered_media()) == 1
    assert len(render_post(post, [Platform.THREADS])[Platform.THREADS].media) == 1


def test_clearing_an_override_falls_back_to_the_post_level_text(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    target = PostTarget.objects.create(post=post, platform=Platform.THREADS)
    set_alt_text(post, media_asset=media_asset, alt_text="Base text")
    set_alt_text(post, media_asset=media_asset, alt_text="Target text", target=target)
    set_alt_text(post, media_asset=media_asset, alt_text="", target=target)

    payload = render_post(post, [Platform.THREADS])[Platform.THREADS].as_dict()
    assert payload["media"][0]["alt"] == "Base text"


def test_replacing_media_does_not_strand_override_rows(
    workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    """A composer sends the whole ordered list on every save. An override row
    for a removed asset would survive as a row pointing at media the post no
    longer carries."""
    from content.services.posts import update_post

    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    target = PostTarget.objects.create(post=post, platform=Platform.THREADS)
    set_alt_text(post, media_asset=media_asset, alt_text="Gone soon", target=target)

    replacement = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))
    update_post(post, media_asset_ids=[replacement])

    assert not PostMediaAttachment.objects.filter(post=post, media_asset=media_asset).exists()


def test_one_base_row_per_asset_is_enforced_by_the_database(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    """Postgres treats repeated NULLs as distinct, so a naive
    `UNIQUE(post, media_asset, target_override)` would silently allow two base
    rows for one asset. The partial constraint is what closes that."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])

    with pytest.raises(IntegrityError), transaction.atomic():
        PostMediaAttachment.objects.create(post=post, media_asset=media_asset, order=1)


def test_one_override_row_per_asset_per_target(workspace: Any, user: Any, media_asset: Any) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    target = PostTarget.objects.create(post=post, platform=Platform.THREADS)
    set_alt_text(post, media_asset=media_asset, alt_text="First", target=target)

    with pytest.raises(IntegrityError), transaction.atomic():
        PostMediaAttachment.objects.create(
            post=post, media_asset=media_asset, target_override=target
        )


# -----------------------------------------------------------------------------
# The endpoint
# -----------------------------------------------------------------------------
def test_setting_alt_text_over_the_api(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])

    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {"media_asset": media_asset.pk, "alt_text": "A blue ceramic mug"},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["media"][0]["alt_text"] == "A blue ceramic mug"


def test_setting_alt_text_for_one_platform_over_the_api(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    PostTarget.objects.create(post=post, platform=Platform.LINKEDIN)

    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {
            "media_asset": media_asset.pk,
            "alt_text": "Catalogue shot",
            "platform": Platform.LINKEDIN,
        },
        format="json",
    )
    assert response.status_code == 200

    rendered = render_post(post, [Platform.LINKEDIN])[Platform.LINKEDIN]
    assert rendered.media[0].alt == "Catalogue shot"


def test_alt_text_for_an_unattached_asset_is_400(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any, make_png_upload: Any
) -> None:
    """A 400, not a silently created row: an asset that is not on the post has
    no *use* to describe."""
    post = create_post(workspace=workspace, author=user)
    detached = ingest_media(workspace=workspace, upload=make_png_upload("b.png"))

    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {"media_asset": detached.pk, "alt_text": "Nope"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "media_not_attached"


def test_alt_text_for_a_platform_with_no_target_is_400(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])

    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {"media_asset": media_asset.pk, "alt_text": "x", "platform": Platform.TIKTOK},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_target_for_platform"


def test_another_workspaces_media_cannot_be_described(
    auth_client: Any, workspace: Any, user: Any, make_png_upload: Any
) -> None:
    """The media id is scoped to the caller's workspace, so a foreign id is
    not a valid choice — it never reaches the attachment lookup."""
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))

    post = create_post(workspace=workspace, author=user)
    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {"media_asset": foreign.pk, "alt_text": "Not yours"},
        format="json",
    )
    assert response.status_code == 400


def test_alt_text_on_another_workspaces_post_is_404(
    auth_client: Any, workspace: Any, make_png_upload: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign_asset = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))
    foreign_post = create_post(workspace=theirs, author=stranger, media_assets=[foreign_asset])

    response = auth_client.post(
        ALT_URL.format(pk=foreign_post.pk),
        {"media_asset": foreign_asset.pk, "alt_text": "Not yours"},
        format="json",
    )
    assert response.status_code == 404


def test_alt_text_on_another_organizations_post_is_404(
    auth_client: Any, workspace: Any, make_png_upload: Any
) -> None:
    """Part 7 rule 3 has three dimensions now; org is the outer one."""
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign_post = create_post(workspace=theirs, author=stranger)
    response = auth_client.post(
        ALT_URL.format(pk=foreign_post.pk), {"media_asset": 1, "alt_text": "x"}, format="json"
    )
    assert response.status_code == 404


def test_alt_text_is_length_capped(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    response = auth_client.post(
        ALT_URL.format(pk=post.pk),
        {"media_asset": media_asset.pk, "alt_text": "x" * 1001},
        format="json",
    )
    assert response.status_code == 400


def test_the_post_serializer_reports_alt_text(
    auth_client: Any, workspace: Any, user: Any, media_asset: Any
) -> None:
    """A composer cannot prompt for missing alt text it cannot read back."""
    post = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    set_alt_text(post, media_asset=media_asset, alt_text="Described")

    body = auth_client.get(f"/api/v1/posts/{post.pk}/").json()
    assert body["media"] == [
        {
            "id": media_asset.pk,
            "kind": media_asset.kind,
            "url": media_asset.file.url,
            "alt_text": "Described",
        }
    ]


def test_ordered_media_survives_an_asset_shared_across_posts(
    workspace: Any, user: Any, media_asset: Any
) -> None:
    """Regression guard for the reverse-accessor trap `ordered_media` already
    documents, now that the join has a second row shape to confuse it with."""
    first = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    second = create_post(workspace=workspace, author=user, media_assets=[media_asset])
    target = PostTarget.objects.create(post=second, platform=Platform.THREADS)
    set_alt_text(second, media_asset=media_asset, alt_text="Only the second", target=target)

    assert [asset.pk for asset in Post.objects.get(pk=first.pk).ordered_media()] == [media_asset.pk]
