"""Crop and trim (P1-12).

**The editor is a new ingestion path, not a mutation path.** `MediaAsset` is
immutable by design — its bytes, checksum and probed dimensions are established
once — so an edit produces a *new* asset carrying `derived_from`, and the
original is untouched. That is not a workaround for immutability; it is what
makes "which file did we actually publish" answerable a year later, when the
post is in a dispute and the crop has been redone twice.

`ImageEditPort` has a real adapter (Pillow, already a dependency) and a fake.
`VideoEditPort` has a fake and **no vendor** — the same shape `VideoProvider`
took under C-11: the port exists, a fresh checkout runs end to end against the
fake, and a production deployment with nothing configured says so plainly
rather than pretending.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from content.editing.base import CropBox, EditedMedia
from content.editing.fake import FakeMediaEditor
from content.editing.pillow import PillowImageEditor
from content.editing.resolve import get_image_editor, get_video_editor
from content.models import MediaAsset, MediaSource
from content.services import editing
from content.services.media import ingest_media
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

CROP_URL = "/api/v1/media/{pk}/crop/"
TRIM_URL = "/api/v1/media/{pk}/trim/"


# -----------------------------------------------------------------------------
# A new asset, never a mutation
# -----------------------------------------------------------------------------
def test_cropping_creates_a_new_asset(workspace: Any, media_asset: Any) -> None:
    cropped = editing.crop_image(media_asset, box=CropBox(left=0, top=0, width=300, height=300))

    assert cropped.pk != media_asset.pk
    assert cropped.derived_from_id == media_asset.pk
    assert cropped.source == MediaSource.DERIVED
    assert (cropped.width, cropped.height) == (300, 300)


def test_the_original_is_untouched(workspace: Any, media_asset: Any) -> None:
    """The property everything else rests on. A checksum that moved would mean
    the asset a published post points at is not the asset that was
    published."""
    before = (media_asset.checksum, media_asset.width, media_asset.height)

    editing.crop_image(media_asset, box=CropBox(left=10, top=10, width=100, height=100))

    media_asset.refresh_from_db()
    assert (media_asset.checksum, media_asset.width, media_asset.height) == before


def test_a_derived_asset_can_itself_be_derived_from(workspace: Any, media_asset: Any) -> None:
    """Crop twice. The chain is what makes the history readable; collapsing it
    to the root would lose the intermediate the user actually chose."""
    once = editing.crop_image(media_asset, box=CropBox(left=0, top=0, width=400, height=400))
    twice = editing.crop_image(once, box=CropBox(left=0, top=0, width=200, height=200))

    assert twice.derived_from_id == once.pk
    assert once.derived_from_id == media_asset.pk


def test_the_derived_asset_belongs_to_the_same_workspace(workspace: Any, media_asset: Any) -> None:
    cropped = editing.crop_image(media_asset, box=CropBox(left=0, top=0, width=100, height=100))
    assert cropped.workspace_id == media_asset.workspace_id


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "box",
    [
        CropBox(left=0, top=0, width=0, height=100),
        CropBox(left=0, top=0, width=100, height=0),
        CropBox(left=-1, top=0, width=100, height=100),
        CropBox(left=500, top=500, width=200, height=200),  # runs off a 600x600
    ],
)
def test_a_crop_outside_the_image_is_rejected(workspace: Any, media_asset: Any, box: Any) -> None:
    with pytest.raises(editing.InvalidEditError):
        editing.crop_image(media_asset, box=box)


def test_cropping_a_video_is_rejected(workspace: Any, user: Any) -> None:
    """Wrong tool, and a 400 says so. Silently routing it to the video editor
    would crop nothing and produce a file nobody asked for."""
    video = MediaAsset.objects.create(
        workspace=workspace, kind="VIDEO", mime="video/mp4", duration_ms=5000
    )
    with pytest.raises(editing.InvalidEditError):
        editing.crop_image(video, box=CropBox(left=0, top=0, width=10, height=10))


def test_trimming_an_image_is_rejected(workspace: Any, media_asset: Any) -> None:
    with pytest.raises(editing.InvalidEditError):
        editing.trim_video(media_asset, start_ms=0, end_ms=1000)


def test_a_trim_that_ends_before_it_starts_is_rejected(workspace: Any, user: Any) -> None:
    video = MediaAsset.objects.create(
        workspace=workspace, kind="VIDEO", mime="video/mp4", duration_ms=5000
    )
    with pytest.raises(editing.InvalidEditError):
        editing.trim_video(video, start_ms=3000, end_ms=1000)


# -----------------------------------------------------------------------------
# The ports
# -----------------------------------------------------------------------------
def test_the_image_editor_resolves_to_something_on_a_fresh_checkout() -> None:
    """Part 7 rule 6: a fresh checkout runs end to end with zero third-party
    accounts."""
    assert get_image_editor() is not None


@override_settings(USE_FAKE_MEDIA_EDITOR=False)
def test_the_real_image_editor_is_pillow() -> None:
    assert isinstance(get_image_editor(), PillowImageEditor)


@override_settings(USE_FAKE_MEDIA_EDITOR=False, VIDEO_EDITOR_BACKEND="")
def test_the_video_editor_may_resolve_to_nothing() -> None:
    """No vendor sits behind it yet, and "not configured" is a deployment fact
    to state plainly, not an exception to catch."""
    assert get_video_editor() is None


@override_settings(USE_FAKE_MEDIA_EDITOR=False, VIDEO_EDITOR_BACKEND="")
def test_trimming_without_a_configured_editor_says_so(workspace: Any) -> None:
    video = MediaAsset.objects.create(
        workspace=workspace, kind="VIDEO", mime="video/mp4", duration_ms=5000
    )
    with pytest.raises(editing.EditorNotConfiguredError):
        editing.trim_video(video, start_ms=0, end_ms=1000)


def test_both_editors_return_the_same_shape(workspace: Any, media_asset: Any) -> None:
    """The contract, asserted across implementations — the same reason
    `test_adapter_contract.py` runs one suite over the fake and the real
    adapter."""
    with media_asset.file.open("rb") as handle:
        content = handle.read()
    box = CropBox(left=0, top=0, width=120, height=90)

    for editor in (FakeMediaEditor(), PillowImageEditor()):
        result = editor.crop(content=content, box=box)
        assert isinstance(result, EditedMedia)
        assert (result.width, result.height) == (120, 90)
        assert result.mime.startswith("image/")
        assert result.content


def test_the_fake_records_what_it_was_asked_for(workspace: Any, media_asset: Any) -> None:
    editor = FakeMediaEditor()
    editor.crop(content=b"x", box=CropBox(left=1, top=2, width=3, height=4))
    assert editor.crops == [(1, 2, 3, 4)]


# -----------------------------------------------------------------------------
# The endpoints
# -----------------------------------------------------------------------------
def test_cropping_over_the_api(auth_client: Any, workspace: Any, media_asset: Any) -> None:
    response = auth_client.post(
        CROP_URL.format(pk=media_asset.pk),
        {"left": 0, "top": 0, "width": 200, "height": 200},
        format="json",
    )
    assert response.status_code == 201

    body = response.json()
    assert body["id"] != media_asset.pk
    assert body["width"] == 200


def test_an_invalid_crop_over_the_api_is_400(
    auth_client: Any, workspace: Any, media_asset: Any
) -> None:
    response = auth_client.post(
        CROP_URL.format(pk=media_asset.pk),
        {"left": 0, "top": 0, "width": 5000, "height": 5000},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_edit"


def test_cropping_another_workspaces_media_is_404(
    auth_client: Any, workspace: Any, make_png_upload: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))

    response = auth_client.post(
        CROP_URL.format(pk=foreign.pk),
        {"left": 0, "top": 0, "width": 10, "height": 10},
        format="json",
    )
    assert response.status_code == 404


def test_cropping_another_organizations_media_is_404(
    auth_client: Any, workspace: Any, make_png_upload: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    assert theirs.organization != workspace.organization

    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))
    response = auth_client.post(
        CROP_URL.format(pk=foreign.pk),
        {"left": 0, "top": 0, "width": 10, "height": 10},
        format="json",
    )
    assert response.status_code == 404


def test_the_derived_asset_is_reported_by_the_api(
    auth_client: Any, workspace: Any, media_asset: Any
) -> None:
    """A composer that cannot see the lineage cannot offer "revert to
    original"."""
    cropped = editing.crop_image(media_asset, box=CropBox(left=0, top=0, width=100, height=100))
    body = auth_client.get(f"/api/v1/media/{cropped.pk}/").json()
    assert body["derived_from"] == media_asset.pk
