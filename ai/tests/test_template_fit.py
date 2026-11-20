"""S17-43 — the template decides which Studio controls may change it
(`studio_flow_1911.md` Step 1b, `studio_controls_1911.md`).

Beta render #7 (2026-11-19): a person template on a beach was sent the Studio's
`scene: seamless-studio`, `cast: product-only` and `camera: eye-level`; the first
frame never saw them, the motion prompt did, and the man vanished. Here the
template locks what it owns, the server refuses what does not fit (400
`not_in_template`, never ignored), and what is left reaches the first frame and
the motion alike.
"""

# ruff: noqa: F811 — pytest fixtures imported from test_ad_templates
from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from ai.models import CreativeKind, CreativeOption, Generation, VideoRender, VideoRenderStatus
from ai.providers.fake import _fake_text_provider
from ai.services import ad_templates, template_fit
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    KEYFRAME,
    RENDERS,
    avatar_for,
    catalog,
    code_of,
    flag_on,
    growth,
    lina,
    make_product,
    oil,
    template_clip,
)
from billing.models import FeatureFlag
from billing.services.flags import STUDIO_REPHRASE
from brand.models import BrandPlace, PlaceSource
from content.services.media import ingest_media
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

#: What the Studio sent with render #7, defaults included.
RENDER_7_LOOK = {
    "scene": "seamless-studio",
    "cast": "product-only",
    "camera": "eye-level",
    "light": "hard-sun",
    "palette": "mediterranean",
    "moods": ["editorial", "warm"],
    "vibe": "everyday",
    "avoid": "jewelry",
}


def row(key: str) -> CreativeOption:
    return CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key=key)


def own_place(workspace: Any, upload: Any, stands_for: str = "") -> BrandPlace:
    media = ingest_media(workspace=workspace, upload=upload("shop.png", (800, 600)))
    return BrandPlace.objects.create(
        workspace=workspace,
        label="Our place",
        source=PlaceSource.UPLOAD,
        media=media,
        stands_for=stands_for,
    )


def keyframe(client: Any, product: Any, **body: Any) -> Any:
    return client.post(
        KEYFRAME, {"template": "beach-day", "product": product.pk, **body}, format="json"
    )


# -----------------------------------------------------------------------------
# What each template allows
# -----------------------------------------------------------------------------
def test_a_template_locks_the_scene_the_camera_and_the_cast(catalog: None) -> None:
    for template in ad_templates._rows():
        controls = template_fit.controls(template)
        for kind in ("scene", "camera", "cast"):
            assert controls[kind]["locked"] is True, (template.key, kind)


def test_beach_day_keeps_its_own_light_and_calm_vibes(catalog: None) -> None:
    controls = template_fit.controls(row("beach-day"))
    assert controls["light"]["locked"] is True
    assert set(controls["moods"]["open"]) <= {"film", "natural"}
    assert "street" not in controls["vibe"]["open"]
    assert "active" not in controls["vibe"]["open"]
    assert controls["palette"]["open"] == "any"
    assert controls["places"]["settings"] == ["beach"]


def test_no_template_offers_a_mood_that_moves_the_camera_or_the_set(catalog: None) -> None:
    banned = {"editorial", "clean", "airy", "moody", "playful"}
    for template in ad_templates._rows():
        moods = template_fit.controls(template)["moods"]
        assert not banned & set(moods.get("open") or []), template.key


def test_an_admin_override_wins_over_the_derived_rule(catalog: None) -> None:
    template = row("beach-day")
    template.metadata = {**template.metadata, "controls": {"moods": {"open": ["film"]}}}
    template.save(update_fields=["metadata"])
    assert template_fit.controls(template)["moods"]["open"] == ["film"]


def test_a_malformed_override_is_refused_on_save(catalog: None) -> None:
    meta = {**row("beach-day").metadata, "controls": {"scene": {"open": "everything"}}}
    with pytest.raises(DjangoValidationError):
        ad_templates.validate_metadata(meta)
    meta = {**row("beach-day").metadata, "controls": {"weather": {"locked": True}}}
    with pytest.raises(DjangoValidationError):
        ad_templates.validate_metadata(meta)


def test_the_gallery_serves_each_templates_controls(auth_client: Any, oil: Any) -> None:
    body = auth_client.get("/api/v1/ai/ad-templates/", {"product": oil.pk}).json()
    beach = next(item for item in body["templates"] if item["key"] == "beach-day")
    assert beach["controls"] == template_fit.served(row("beach-day"))
    assert beach["controls"]["scene"]["locked"] is True
    assert beach["controls"]["scene"]["reason"] == "template_place"
    assert beach["controls"]["palette"]["any"] is True


# -----------------------------------------------------------------------------
# The server refuses what does not fit
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("look", "control"),
    [
        ({"cast": "product-only"}, "cast"),
        ({"scene": "seamless-studio"}, "scene"),
        ({"camera": "eye-level"}, "camera"),
        ({"light": "hard-sun"}, "light"),
        ({"moods": ["editorial"]}, "moods"),
        ({"vibe": "street"}, "vibe"),
    ],
)
def test_a_choice_the_template_does_not_take_is_400(
    auth_client: Any, oil: Any, lina: Any, look: dict[str, Any], control: str
) -> None:
    response = keyframe(auth_client, oil, avatar=lina.pk, creative=look)
    assert response.status_code == 400, response.json()
    assert code_of(response) == "not_in_template"
    assert response.json()["error"]["detail"]["control"] == control


def test_the_estimate_and_the_render_refuse_it_too(auth_client: Any, oil: Any, lina: Any) -> None:
    body = template_clip(oil, template="beach-day", avatar=lina.pk, creative=RENDER_7_LOOK)
    response = auth_client.post(ESTIMATE, body, format="json")
    assert response.status_code == 400
    assert code_of(response) == "not_in_template"
    response = auth_client.post(RENDERS, {**body, "estimate_id": "est_x"}, format="json")
    assert response.status_code == 400
    assert code_of(response) == "not_in_template"


def test_what_the_template_leaves_open_is_accepted(auth_client: Any, oil: Any, lina: Any) -> None:
    look = {"palette": "mediterranean", "moods": ["film"], "vibe": "everyday", "avoid": "jewelry"}
    response = keyframe(auth_client, oil, avatar=lina.pk, creative=look)
    assert response.status_code == 201, response.json()


def test_a_vibe_for_people_needs_the_avatar_on(
    auth_client: Any, oil: Any, lina: Any, catalog: None
) -> None:
    optional = next(
        template
        for template in ad_templates._rows()
        if template.metadata["person"] == "optional"
        and "luxe" in template_fit.controls(template)["vibe"]["open"]
        and "luxe" not in template_fit.controls(template)["vibe"]["product_only"]
        and not template.metadata.get("hidden")
    )
    body = {"template": optional.key, "product": oil.pk, "creative": {"vibe": "luxe"}}
    alone = auth_client.post(KEYFRAME, body, format="json")
    assert alone.status_code == 400
    assert alone.json()["error"]["detail"]["control"] == "vibe"
    assert auth_client.post(KEYFRAME, {**body, "avatar": lina.pk}, format="json").status_code == 201


def test_the_brand_colour_set_takes_only_the_brands_palette(catalog: None) -> None:
    template = next(
        t
        for t in ad_templates._rows()
        if "brand-colour" in t.metadata["locations"] and len(t.metadata["locations"]) > 1
    )
    with pytest.raises(template_fit.NotInTemplateError) as refused:
        template_fit.check(
            template, {"palette": "pastel"}, location="brand-colour", person_on=False
        )
    assert refused.value.payload["control"] == "palette"
    assert template_fit.check(
        template, {"palette": "brand"}, location="brand-colour", person_on=False
    ) == {"palette": "brand"}


def test_an_open_light_is_checked_against_the_place_picked(catalog: None) -> None:
    template, place, light = next(
        (t, place, key)
        for t in ad_templates._rows()
        for key in (template_fit.controls(t)["light"].get("open") or [])
        for place in t.metadata["locations"]
        if place in template_fit.light_conflicts(key)
    )
    with pytest.raises(template_fit.NotInTemplateError) as refused:
        template_fit.check(template, {"light": light}, location=place, person_on=False)
    assert refused.value.payload["control"] == "light"


# -----------------------------------------------------------------------------
# Own places carry what they stand for
# -----------------------------------------------------------------------------
def test_an_own_kitchen_does_not_fit_a_beach_template(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any
) -> None:
    kitchen = own_place(oil.workspace, make_png_upload, stands_for="kitchen")
    response = keyframe(auth_client, oil, avatar=lina.pk, location=f"own:{kitchen.pk}")
    assert response.status_code == 400
    assert code_of(response) == "not_in_template"
    assert response.json()["error"]["detail"]["control"] == "place"


def test_an_own_beach_fits_a_beach_template(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any
) -> None:
    beach = own_place(oil.workspace, make_png_upload, stands_for="seaside")
    response = keyframe(auth_client, oil, avatar=lina.pk, location=f"own:{beach.pk}")
    assert response.status_code == 201, response.json()


def test_an_untagged_own_place_fits_no_template_until_it_is_told_what_it_is(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any
) -> None:
    place = own_place(oil.workspace, make_png_upload)
    response = keyframe(auth_client, oil, avatar=lina.pk, location=f"own:{place.pk}")
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["reason"] == "place_untagged"
    tagged = auth_client.patch(
        f"/api/v1/brand/places/{place.pk}/", {"stands_for": "beach"}, format="json"
    )
    assert tagged.status_code == 200, tagged.json()
    assert keyframe(auth_client, oil, avatar=lina.pk, location=f"own:{place.pk}").status_code == 201


def test_a_place_cannot_stand_for_an_unknown_place(
    auth_client: Any, oil: Any, make_png_upload: Any
) -> None:
    place = own_place(oil.workspace, make_png_upload)
    response = auth_client.patch(
        f"/api/v1/brand/places/{place.pk}/", {"stands_for": "moon"}, format="json"
    )
    assert response.status_code == 400


def test_another_brands_place_is_404_and_cannot_be_tagged(
    auth_client: Any, oil: Any, lina: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    for workspace in (
        provision_workspace(other_user, name="Stranger"),
        provision_extra_workspace(user=user, name="Sibling"),
    ):
        theirs = own_place(workspace, make_png_upload, stands_for="beach")
        here = {"HTTP_X_WORKSPACE_ID": str(oil.workspace.pk)}
        response = auth_client.post(
            KEYFRAME,
            {
                "template": "beach-day",
                "product": oil.pk,
                "avatar": lina.pk,
                "location": f"own:{theirs.pk}",
            },
            format="json",
            **here,
        )
        assert response.status_code == 404
        response = auth_client.patch(
            f"/api/v1/brand/places/{theirs.pk}/", {"stands_for": "kitchen"}, format="json", **here
        )
        assert response.status_code == 404


def test_the_gallery_says_which_own_places_fit_each_template(
    auth_client: Any, oil: Any, make_png_upload: Any
) -> None:
    beach = own_place(oil.workspace, make_png_upload, stands_for="beach")
    kitchen = own_place(oil.workspace, make_png_upload, stands_for="kitchen")
    body = auth_client.get("/api/v1/ai/ad-templates/", {"product": oil.pk}).json()
    own = {item["key"]: item for item in body["own_places"]}
    assert own[f"own:{beach.pk}"]["stands_for"] == "beach"
    template = next(item for item in body["templates"] if item["key"] == "beach-day")
    assert template["own_places"] == [f"own:{beach.pk}"]
    assert f"own:{kitchen.pk}" not in template["own_places"]


# -----------------------------------------------------------------------------
# Render #7: the first frame and the motion read the same instructions
# -----------------------------------------------------------------------------
def test_render_7_compiles_without_the_studio_look_it_never_showed(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    allowed = template_fit.allowed_part(
        row("beach-day"), RENDER_7_LOOK, location="beach", person_on=True
    )
    assert allowed == {"palette": "mediterranean", "vibe": "everyday", "avoid": "jewelry"}
    body = template_clip(oil, template="beach-day", avatar=lina.pk, creative=allowed)
    estimate = auth_client.post(ESTIMATE, body, format="json")
    assert estimate.status_code == 200, estimate.json()
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate.json()["estimate_id"]}, format="json"
    )
    assert response.status_code == 202, response.json()
    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.status == VideoRenderStatus.SUCCEEDED, render.error
    frame = Generation.objects.get(pk=render.keyframe_id)
    for prompt in (render.prompt_sent, frame.prompt_sent):
        lowered = prompt.lower()
        for phrase in ("no people", "seamless", "eye level", "only subject", "#", "hashtag"):
            assert phrase not in lowered, (phrase, prompt)
    # The palette the person kept is drawn into the first frame, and the
    # motion keeps that frame rather than describing it again.
    assert "cobalt" in frame.prompt_sent.lower()
    assert "image 1 is the first frame: keep its place" in render.prompt_sent.lower()


def test_a_render_made_first_frame_gets_the_persons_words(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    body = template_clip(oil, template="beach-day", avatar=lina.pk, prompt="he wears a red cap")
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    render = VideoRender.objects.get(pk=response.json()["id"])
    frame = Generation.objects.get(pk=render.keyframe_id)
    assert "red cap" in frame.prompt_sent
    assert "red cap" in render.prompt_sent


def test_a_template_frame_and_render_are_never_rewritten(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    FeatureFlag.objects.create(organization=None, key=STUDIO_REPHRASE, enabled=True)
    _fake_text_provider.calls.clear()
    body = template_clip(oil, template="beach-day", avatar=lina.pk)
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    render = VideoRender.objects.get(pk=response.json()["id"])
    frame = Generation.objects.get(pk=render.keyframe_id)
    assert frame.rephrased_by == "" and frame.rephrased_prompt == ""
    assert render.rephrased_by in ("", "template")
    from ai.services.rephrase import IMAGE_SYSTEM, MOTION_SYSTEM

    systems = {IMAGE_SYSTEM, MOTION_SYSTEM}
    assert [call for call in _fake_text_provider.calls if call["system"] in systems] == []


def test_every_template_render_compiles_its_timed_direction_without_pro(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    body = template_clip(oil, template="beach-day", avatar=lina.pk)
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    prompt = VideoRender.objects.get(pk=response.json()["id"]).prompt_sent
    # Beats as actions on the clock, never the label list ("Call to action").
    assert "0-" in prompt and "call to action" not in prompt.lower()
