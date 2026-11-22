"""S20 — the Studio storyboard (`studio_storyboard.md`).

A template's timed beats are its scenes. With `studio_storyboard_s20` on, the
person may reorder, retime, drop, add (a ready-made `story_scene` row or their
own words) and reword them; the edited list becomes the beats of the one
directed render, at the same price. Refused before any charge when it cannot
be rendered as asked. Flag off is the Studio as it was: no scenes served, and a
`storyboard` in the request is a 400, never silently ignored.
"""

# ruff: noqa: F811 — pytest fixtures imported from test_ad_templates
from __future__ import annotations

from typing import Any

import pytest

from ai.models import CreativeKind, CreativeOption, VideoRender
from ai.providers.fake import _fake_video_provider
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    GALLERY,
    RENDERS,
    avatar_for,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    lina,
    make_product,
    oil,
    on_plan,
    template_clip,
)
from ai.tests.test_talking_templates import LINES
from billing.models import FeatureFlag
from billing.services.flags import STUDIO_STORYBOARD_S20
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

OPTIONS = "/api/v1/ai/creative-options/"


@pytest.fixture
def board_on(db: None) -> None:
    FeatureFlag.objects.create(organization=None, key=STUDIO_STORYBOARD_S20, enabled=True)


def unbox(product: Any, storyboard: list[dict[str, Any]] | None, **over: Any) -> dict[str, Any]:
    body = template_clip(product, template="slow-unbox", length="clip-10", **over)
    if storyboard is not None:
        body["storyboard"] = storyboard
    return body


#: slow-unbox's own four beats, reordered, one reworded, one ready-made added.
EDITED = [
    {"scene": "beat:1", "seconds": 2},
    {"scene": "pour", "seconds": 3, "words": "pouring over a fresh salad"},
    {"scene": "beat:2", "seconds": 3},
    {"scene": "custom", "seconds": 2, "words": "a family picnic by the sea"},
]


def render(client: Any, body: dict[str, Any]) -> VideoRender:
    response = client.post(RENDERS, confirmed(client, body), format="json")
    assert response.status_code == 202, response.json()
    made = VideoRender.objects.get(pk=response.json()["id"])
    assert made.status == "SUCCEEDED", (made.error_code, made.error)
    return made


# -----------------------------------------------------------------------------
# S20-01 — the ready-made scenes
# -----------------------------------------------------------------------------
def test_the_ready_made_scenes_are_rows_served_with_the_flag_on(
    auth_client: Any, growth: Any, board_on: None
) -> None:
    rows = CreativeOption.objects.filter(kind=CreativeKind.STORY_SCENE, is_active=True)
    assert rows.count() >= 12
    served = auth_client.get(OPTIONS).json()["options"]["story_scene"]
    pour = next(row for row in served if row["key"] == "pour")
    assert pour["label"] == "The pour"
    assert pour["metadata"]["person"] == "optional"
    assert pour["metadata"]["image"].startswith("/studio/")
    assert pour["metadata"]["suggestions"]["en"]
    # The model's words stay on the server, as every fragment does.
    assert "prompt_fragment" not in pour


def test_the_scenes_speak_french_and_italian(
    auth_client: Any, catalog: None, board_on: None
) -> None:
    row = CreativeOption.objects.get(kind=CreativeKind.STORY_SCENE, key="pour")
    assert row.translations["fr"]["label"] == "Le versement"
    assert row.translations["it"]["label"] == "Il versamento"


def test_with_the_flag_off_no_scene_is_served(auth_client: Any, growth: Any) -> None:
    assert auth_client.get(OPTIONS).json()["options"]["story_scene"] == []


# -----------------------------------------------------------------------------
# The gallery: each template's scenes
# -----------------------------------------------------------------------------
def test_the_gallery_lists_a_templates_scenes_with_the_flag_on(
    auth_client: Any, oil: Any, board_on: None
) -> None:
    body = auth_client.get(GALLERY, {"product": oil.pk}).json()
    assert body["storyboard"] is True
    rows = {row["key"]: row for row in body["templates"]}
    scenes = rows["slow-unbox"]["scenes"]
    # The template's own names where it has one per beat, the action's first words after.
    assert [scene["label"] for scene in scenes] == [
        "Box on the table",
        "Lid lifts",
        "Product in light",
        "Hold",
    ]
    assert scenes[0]["start"] == 0 and scenes[-1]["end"] == 1
    assert all(scene["action"] and "{" not in scene["action"] for scene in scenes)
    # A multi-shot template's scenes are fixed shots: none to edit.
    assert rows["value-stack"]["scenes"] == []


def test_with_the_flag_off_the_gallery_has_no_storyboard(auth_client: Any, oil: Any) -> None:
    body = auth_client.get(GALLERY, {"product": oil.pk}).json()
    assert body["storyboard"] is False
    assert all(row["scenes"] == [] for row in body["templates"])


# -----------------------------------------------------------------------------
# S20-02 — the happy path
# -----------------------------------------------------------------------------
def test_an_edited_storyboard_becomes_the_renders_beats(
    auth_client: Any, oil: Any, board_on: None
) -> None:
    _fake_video_provider.clear()
    made = render(auth_client, unbox(oil, EDITED))

    prompt = made.prompt_sent
    assert "pouring over a fresh salad" in prompt
    assert "a family picnic by the sea" in prompt
    assert "the lid lifts straight up" in prompt
    # Dropped: the template's first and last beats.
    assert "a hand resting on the lid" not in prompt
    # In the order asked: the lid before the salad before the picnic.
    assert prompt.index("the lid lifts") < prompt.index("fresh salad") < prompt.index("picnic")
    assert made.spec["storyboard"][1] == {
        "scene": "pour",
        "seconds": 3.0,
        "words": "pouring over a fresh salad",
        "person": False,
    }
    assert len(_fake_video_provider.requests) == 1


def test_the_storyboard_costs_what_the_template_costs(
    auth_client: Any, oil: Any, board_on: None
) -> None:
    plain = auth_client.post(ESTIMATE, unbox(oil, None), format="json").json()
    edited = auth_client.post(ESTIMATE, unbox(oil, EDITED), format="json").json()
    assert (edited["credits"], edited["units"]) == (plain["credits"], plain["units"])


def test_a_person_in_a_scene_needs_the_ad_to_have_one(
    auth_client: Any, oil: Any, lina: Any, board_on: None
) -> None:
    scenes = [{"scene": "beat:0", "seconds": 4}, {"scene": "pour", "seconds": 6, "person": True}]
    response = auth_client.post(ESTIMATE, unbox(oil, scenes), format="json")
    assert response.status_code == 400 and code_of(response) == "storyboard_invalid"

    _fake_video_provider.clear()
    made = render(auth_client, unbox(oil, scenes, avatar=lina.pk))
    assert "the person is in the shot" in made.prompt_sent.lower()


def test_without_a_storyboard_nothing_changes(auth_client: Any, oil: Any, board_on: None) -> None:
    before = render(auth_client, unbox(oil, None)).prompt_sent
    FeatureFlag.objects.filter(key=STUDIO_STORYBOARD_S20).update(enabled=False)
    assert render(auth_client, unbox(oil, None)).prompt_sent == before


# -----------------------------------------------------------------------------
# Every error code
# -----------------------------------------------------------------------------
def test_with_the_flag_off_a_storyboard_is_refused(auth_client: Any, oil: Any) -> None:
    response = auth_client.post(ESTIMATE, unbox(oil, EDITED), format="json")
    assert response.status_code == 400
    assert code_of(response) == "storyboard_off"


@pytest.mark.parametrize(
    ("scenes", "why"),
    [
        ([{"scene": "beat:0", "seconds": 4}], "seconds"),
        ([{"scene": "beat:0", "seconds": 0.5}, {"scene": "beat:1", "seconds": 9.5}], "seconds"),
        ([{"scene": "beat:0", "seconds": 1.5}] * 6 + [{"scene": "beat:1", "seconds": 1}], "count"),
        ([], "count"),
        ([{"scene": "beat:9", "seconds": 10}], "scene"),
        ([{"scene": "nope", "seconds": 10}], "scene"),
        ([{"scene": "custom", "seconds": 10}], "words"),
        ([{"scene": "talk", "seconds": 10}], "person"),
        ([{"scene": "custom", "seconds": 10, "words": "#sale this week"}], "words"),
        ([{"scene": "custom", "seconds": 10, "words": "hard cut to the logo"}], "words"),
    ],
)
def test_a_storyboard_that_cannot_be_rendered_is_400(
    scenes: list[dict[str, Any]], why: str, auth_client: Any, oil: Any, board_on: None
) -> None:
    response = auth_client.post(ESTIMATE, unbox(oil, scenes), format="json")
    assert response.status_code == 400, response.json()
    assert code_of(response) == "storyboard_invalid"
    assert response.json()["error"]["detail"]["field"] == why
    assert not VideoRender.objects.exists()


def test_a_multi_shot_template_has_fixed_scenes(auth_client: Any, oil: Any, board_on: None) -> None:
    body = template_clip(oil, template="value-stack", length="clip-10")
    body["storyboard"] = [{"scene": "beat:0", "seconds": 10}]
    response = auth_client.post(ESTIMATE, body, format="json")
    assert response.status_code == 400 and code_of(response) == "storyboard_invalid"
    assert response.json()["error"]["detail"]["field"] == "template"


def test_a_talking_template_keeps_every_spoken_beat(
    auth_client: Any, oil: Any, lina: Any, board_on: None
) -> None:
    body = {
        **template_clip(oil, template="founder-story", length="clip-10", avatar=lina.pk),
        "script": dict(LINES["founder-story"]),
        "storyboard": [{"scene": "beat:0", "seconds": 10}],
    }
    response = auth_client.post(ESTIMATE, body, format="json")
    assert response.status_code == 400 and code_of(response) == "storyboard_invalid"
    assert response.json()["error"]["detail"]["field"] == "speech"


def test_own_words_need_the_plans_prompt_edit(
    auth_client: Any, oil: Any, plans: dict[str, Any], board_on: None
) -> None:
    on_plan(oil.workspace, plans, "spark")
    scenes = [{"scene": "custom", "seconds": 10, "words": "a picnic"}]
    response = auth_client.post(ESTIMATE, unbox(oil, scenes), format="json")
    assert response.status_code == 402


# -----------------------------------------------------------------------------
# Tenancy: another brand's product or avatar is 404, storyboard or not
# -----------------------------------------------------------------------------
def test_another_organizations_and_another_brands_product_are_404(
    auth_client: Any, oil: Any, user: Any, other_user: Any, make_png_upload: Any, board_on: None
) -> None:
    stranger = provision_workspace(other_user, name="Stranger Co")
    sibling = provision_extra_workspace(user=user, name="Second brand")
    here = {"HTTP_X_WORKSPACE_ID": str(oil.workspace.pk)}
    for workspace in (stranger, sibling):
        theirs = make_product(workspace, make_png_upload)
        response = auth_client.post(ESTIMATE, unbox(theirs, EDITED), format="json", **here)
        assert response.status_code == 404


def test_another_brands_avatar_is_404(
    auth_client: Any, oil: Any, user: Any, make_png_upload: Any, board_on: None
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second brand")
    theirs = avatar_for(sibling, make_png_upload)
    here = {"HTTP_X_WORKSPACE_ID": str(oil.workspace.pk)}
    scenes = [{"scene": "beat:0", "seconds": 4}, {"scene": "pour", "seconds": 6, "person": True}]
    response = auth_client.post(
        ESTIMATE, unbox(oil, scenes, avatar=theirs.pk), format="json", **here
    )
    assert response.status_code == 404
