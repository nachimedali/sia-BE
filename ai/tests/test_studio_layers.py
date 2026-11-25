"""S21-02 — layers: the Studio's 2D pass over a finished clip or still.

Message bubbles, notifications, comment replies, product tags, the logo, an end
card and each scene's caption are drawn in code (PIL) with the brand's colours
and composited over the render — never asked of a model, so the words stay
sharp and changing them costs nothing. The plain clip is kept: a second pass
starts from it, and an empty pass gives it back. Refused while the render is
in flight or once it is on a post, so the post's media never changes under it.
"""

# ruff: noqa: F811 — pytest fixtures imported from test_ad_templates
from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from ai.models import VideoRender
from ai.providers.fake import _fake_video_composer, read_fake_manifest
from ai.services import layers
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    make_product,
    oil,
    template_clip,
)
from content.models import MediaAsset
from content.services.media import ingest_media
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

IMAGE = "/api/v1/ai/layers/image/"
KIT = {"primary": "#1E1745", "accent": "#F2A65A", "logo": False}

BUBBLE = {
    "kind": "message",
    "text": "Did you try it??",
    "name": "Sarah",
    "side": "left",
    "x": 8,
    "y": 18,
    "w": 66,
    "start": 0.3,
    "end": None,
}
TAG = {
    "kind": "tag",
    "text": "Cold-pressed · 32 TND",
    "x": 44,
    "y": 24,
    "w": 50,
    "start": 0.4,
    "end": 4,
}
CAPTION = {
    "kind": "caption",
    "text": "Tired of flat olive oil?",
    "style": "bold",
    "x": 6,
    "y": 70,
    "w": 88,
    "start": 0,
    "end": 2.5,
}


def png(size: tuple[int, int] = (1080, 1920)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 180, 160)).save(out, format="PNG")
    return out.getvalue()


def still(workspace: Any, size: tuple[int, int] = (1080, 1920)) -> MediaAsset:
    upload = SimpleUploadedFile("still.png", png(size), content_type="image/png")
    return ingest_media(workspace=workspace, upload=upload)


def rendered(client: Any, product: Any) -> VideoRender:
    response = client.post(
        RENDERS, confirmed(client, template_clip(product, template="slow-unbox")), format="json"
    )
    assert response.status_code == 202, response.json()
    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.status == "SUCCEEDED", (render.error_code, render.error)
    return render


def apply(client: Any, render: VideoRender, items: list[dict[str, Any]], **extra: Any) -> Any:
    return client.post(
        f"{RENDERS}{render.pk}/layers/", {"layers": items, "kit": KIT}, format="json", **extra
    )


# -----------------------------------------------------------------------------
# Drawing
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(layers.KINDS))
def test_every_kind_draws_a_transparent_png_inside_the_frame(kind: str) -> None:
    item = layers.clean([{**BUBBLE, "kind": kind, "style": "boxed"}], seconds=10)[0]
    drawn = layers.draw(
        item, frame=(1080, 1920), kit=layers.Kit.of(KIT), logo=None, brand="Saveurs"
    )
    with drawn.image as picture:
        assert picture.mode == "RGBA"
        assert 0 < picture.width <= 1080
        assert drawn.x + picture.width <= 1080
        assert drawn.y + picture.height <= 1920
    assert drawn.x == round(1080 * 0.08)


def test_a_logo_layer_uses_the_real_logo_only_when_the_kit_says_so() -> None:
    item = layers.clean([{**TAG, "kind": "logo"}], seconds=10)[0]
    logo = png((400, 200))
    on = layers.Kit.of({**KIT, "logo": True})
    drawn = layers.draw(item, frame=(1080, 1920), kit=on, logo=logo, brand="Saveurs")
    with drawn.image as picture:
        assert picture.width == round(1080 * 0.5)
        assert picture.height == picture.width // 2
    off = layers.draw(item, frame=(1080, 1920), kit=layers.Kit.of(KIT), logo=logo, brand="Saveurs")
    with off.image as pill:
        assert pill.height != pill.width // 2  # the brand's name on its colour instead


@pytest.mark.parametrize(
    ("item", "field"),
    [
        ({**BUBBLE, "kind": "imessage"}, "kind"),
        ({**BUBBLE, "text": ""}, "text"),
        ({**BUBBLE, "text": "x" * 201}, "text"),
        ({**BUBBLE, "x": 101}, "x"),
        ({**BUBBLE, "w": 5}, "w"),
        ({**BUBBLE, "start": 3, "end": 2}, "end"),
        ({**CAPTION, "style": "neon"}, "style"),
        ({**BUBBLE, "start": 12}, "start"),
    ],
)
def test_a_bad_layer_is_400_naming_its_field(item: dict[str, Any], field: str) -> None:
    with pytest.raises(layers.LayersInvalidError) as raised:
        layers.clean([item], seconds=10)
    assert raised.value.payload == {"index": 0, "field": field}


def test_too_many_layers_is_400() -> None:
    with pytest.raises(layers.LayersInvalidError) as raised:
        layers.clean([TAG] * (layers.MAX_LAYERS + 1), seconds=10)
    assert raised.value.payload["field"] == "layers"


# -----------------------------------------------------------------------------
# Stills
# -----------------------------------------------------------------------------
def test_a_still_gets_a_new_derived_picture_at_its_aspect(auth_client: Any, workspace: Any) -> None:
    source = still(workspace)
    response = auth_client.post(
        IMAGE,
        {"media": source.pk, "aspect": "4:5", "layers": [BUBBLE, TAG], "kit": KIT},
        format="json",
    )
    assert response.status_code == 201, response.json()
    made = MediaAsset.objects.get(pk=response.json()["id"])
    assert made.pk != source.pk
    assert made.derived_from_id == source.pk
    assert (made.width, made.height) == (1080, 1350)


def test_a_still_with_no_layers_is_still_cropped_to_its_aspect(
    auth_client: Any, workspace: Any
) -> None:
    source = still(workspace, (1080, 1080))
    response = auth_client.post(
        IMAGE, {"media": source.pk, "aspect": "9:16", "layers": [], "kit": KIT}, format="json"
    )
    assert response.status_code == 201
    made = MediaAsset.objects.get(pk=response.json()["id"])
    assert (made.width, made.height) == (1080, 1920)


def test_still_refusals_have_their_codes(auth_client: Any, workspace: Any) -> None:
    source = still(workspace)
    bad_aspect = auth_client.post(
        IMAGE, {"media": source.pk, "aspect": "16:9", "layers": [], "kit": KIT}, format="json"
    )
    assert bad_aspect.status_code == 400
    bad_layer = auth_client.post(
        IMAGE,
        {"media": source.pk, "aspect": "9:16", "layers": [{**TAG, "kind": "x"}], "kit": KIT},
        format="json",
    )
    assert bad_layer.status_code == 400
    assert code_of(bad_layer) == "layers_invalid"


def test_another_organizations_and_another_brands_still_are_404(
    auth_client: Any, workspace: Any, user: Any, other_user: Any
) -> None:
    theirs = still(provision_workspace(other_user, name="Stranger Co"))
    sibling = still(provision_extra_workspace(user=user, name="Second brand"))
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    for media in (theirs, sibling):
        response = auth_client.post(
            IMAGE,
            {"media": media.pk, "aspect": "9:16", "layers": [], "kit": KIT},
            format="json",
            **here,
        )
        assert response.status_code == 404


def test_a_viewer_cannot_lay_out_a_still(
    client_as: Any, viewer_user: Any, advanced_workspace: Any
) -> None:
    source = still(advanced_workspace)
    viewer = client_as(viewer_user)
    response = viewer.post(
        IMAGE, {"media": source.pk, "aspect": "9:16", "layers": [], "kit": KIT}, format="json"
    )
    assert response.status_code == 403


# -----------------------------------------------------------------------------
# Clips
# -----------------------------------------------------------------------------
def test_layers_go_over_the_clip_and_keep_the_plain_one(auth_client: Any, oil: Any) -> None:
    render = rendered(auth_client, oil)
    plain = render.output_id
    held = render.unit_charge_id

    response = apply(auth_client, render, [BUBBLE, TAG, CAPTION])
    assert response.status_code == 202, response.json()
    render.refresh_from_db()
    assert render.layers_status == ""
    assert render.plain_id == plain
    assert render.output_id not in (None, plain)
    assert render.unit_charge_id == held  # free: nothing more taken
    assert [layer["kind"] for layer in render.layers] == ["message", "tag", "caption"]
    manifest = read_fake_manifest(render.output.file.read())
    assert manifest["kind"] == "layers"
    assert [round(layer["start"], 1) for layer in manifest["layers"]] == [0.3, 0.4, 0.0]
    assert response.json()["layers"][0]["text"] == "Did you try it??"

    # A second pass starts from the plain clip, never from the first pass.
    calls = len(_fake_video_composer.calls)
    assert apply(auth_client, render, [TAG]).status_code == 202
    plain_bytes = MediaAsset.objects.get(pk=plain).file.read()
    assert _fake_video_composer.calls[calls]["clip"] == hashlib.sha256(plain_bytes).hexdigest()[:16]
    # No layers gives the plain clip back.
    assert apply(auth_client, render, []).status_code == 202
    render.refresh_from_db()
    assert render.output_id == plain


def test_layers_wait_for_the_render_and_stop_once_it_is_sent(auth_client: Any, oil: Any) -> None:
    render = rendered(auth_client, oil)
    VideoRender.objects.filter(pk=render.pk).update(status="RUNNING")
    running = apply(auth_client, render, [TAG])
    assert running.status_code == 409
    assert code_of(running) == "render_not_ready"

    VideoRender.objects.filter(pk=render.pk).update(status="SUCCEEDED", review="ACCEPTED")
    sent = auth_client.post(f"{RENDERS}send/", {"renders": [render.pk]}, format="json")
    assert sent.status_code == 201, sent.json()
    after = apply(auth_client, render, [TAG])
    assert after.status_code == 409
    assert code_of(after) == "render_sent"


def test_a_render_cannot_be_sent_while_its_layers_are_drawn(auth_client: Any, oil: Any) -> None:
    render = rendered(auth_client, oil)
    VideoRender.objects.filter(pk=render.pk).update(review="ACCEPTED", layers_status="running")
    response = auth_client.post(f"{RENDERS}send/", {"renders": [render.pk]}, format="json")
    assert response.status_code == 409
    assert code_of(response) == "layers_running"


def test_a_layer_past_the_clip_is_400(auth_client: Any, oil: Any) -> None:
    render = rendered(auth_client, oil)
    response = apply(auth_client, render, [{**TAG, "start": 30, "end": None}])
    assert response.status_code == 400
    assert code_of(response) == "layers_invalid"


def test_another_organizations_and_another_brands_render_are_404(
    auth_client: Any, oil: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    stranger = provision_workspace(other_user, name="Stranger Co")
    sibling = provision_extra_workspace(user=user, name="Second brand")
    for workspace in (stranger, sibling):
        theirs = VideoRender.objects.create(
            workspace=workspace,
            created_by=workspace.organization.owner,
            product=make_product(workspace, make_png_upload),
            mode="clip",
            spec={"seconds": 10},
            estimate_id="x",
            credits=0,
            status="SUCCEEDED",
        )
        response = apply(auth_client, theirs, [TAG], HTTP_X_WORKSPACE_ID=str(oil.workspace_id))
        assert response.status_code == 404


# -----------------------------------------------------------------------------
# A still to the drafts
# -----------------------------------------------------------------------------
SEND = "/api/v1/ai/layers/image/send/"


def test_a_still_goes_to_the_drafts_as_the_studios_own_work(
    auth_client: Any, workspace: Any
) -> None:
    from content.models import Post

    made = auth_client.post(
        IMAGE,
        {"media": still(workspace).pk, "aspect": "9:16", "layers": [TAG], "kit": KIT},
        format="json",
    ).json()
    response = auth_client.post(
        SEND,
        {"media": made["id"], "text": "Cold-pressed in Djerba", "platforms": ["instagram"]},
        format="json",
    )
    assert response.status_code == 201, response.json()
    post = Post.objects.get(pk=response.json()["post"])
    assert post.status == "DRAFT"
    assert post.source == "AI"
    assert post.master_body == "Cold-pressed in Djerba"
    assert list(post.media_assets.values_list("pk", flat=True)) == [made["id"]]
    assert post.planned_platforms == ["instagram"]


def test_a_still_with_a_drawn_person_carries_the_label(
    auth_client: Any, workspace: Any, monkeypatch: Any
) -> None:
    from ai.services import disclosure
    from content.models import Post

    monkeypatch.setattr(disclosure, "shows_drawn_person", lambda asset: True)
    media = still(workspace)
    response = auth_client.post(
        SEND, {"media": media.pk, "text": "Hi", "language": "fr"}, format="json"
    )
    assert response.status_code == 201
    assert Post.objects.get(pk=response.json()["post"]).master_body == "Hi\n\nGénéré par IA"


def test_another_organizations_and_another_brands_still_cannot_be_sent(
    auth_client: Any, workspace: Any, user: Any, other_user: Any
) -> None:
    theirs = still(provision_workspace(other_user, name="Stranger Co"))
    sibling = still(provision_extra_workspace(user=user, name="Second brand"))
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    for media in (theirs, sibling):
        response = auth_client.post(SEND, {"media": media.pk}, format="json", **here)
        assert response.status_code == 404


def test_a_viewer_cannot_send_a_still(
    client_as: Any, viewer_user: Any, advanced_workspace: Any
) -> None:
    media = still(advanced_workspace)
    assert client_as(viewer_user).post(SEND, {"media": media.pk}, format="json").status_code == 403
