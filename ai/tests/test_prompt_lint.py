"""S17-43 — the prompt check, and the whole catalogue held to it
(`studio_flow_1911.md` Step 3).

Every template, with and without a person where it allows one, compiled for
every video model profile exactly as a render compiles it — and its first
frame as the image model gets it — must pass `prompt_lint`. A template whose
own words contradict themselves fails here, in CI, before any customer pays
for it. Nothing is generated: the words are only compiled.
"""

# ruff: noqa: F811 — pytest fixtures imported from test_ad_templates
from __future__ import annotations

from typing import Any

import pytest

from ai.models import Generation, VideoModelProfile, VideoRender
from ai.providers.fake import _fake_video_provider
from ai.services import ad_templates, prompt_lint, prompting, video_prompt
from ai.services.video import MOVING_USES
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    RENDERS,
    avatar_for,
    catalog,
    flag_on,
    growth,
    make_product,
    template_clip,
)
from brand.services import avatars

pytestmark = pytest.mark.django_db


# -----------------------------------------------------------------------------
# The check
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("prompt", "person", "found"),
    [
        ("People: no people in the frame, a clean still life.", True, "removes the person"),
        ("Brand hashtags: #hello_moto.", False, "caption instruction"),
        ("Use 1 to 3 hashtags.", False, "caption instruction"),
        ('Close with one of these calls to action: "book-now".', False, "caption instruction"),
        ("Timing: 0-2 s: Lying on the towel; 8-10 s: Call to action.", False, "label"),
    ],
)
def test_each_contradiction_is_named(prompt: str, person: bool, found: str) -> None:
    problems = prompt_lint.problems(prompt, person=person)
    assert any(found in problem for problem in problems), problems


@pytest.mark.parametrize(
    "prompt",
    [
        "No people in the frame, a clean still life.",  # fine with nobody in it
        "Brand colours: primary #01623A, accent #3a4a86.",  # hex, not a hashtag
        "Avoid: no text, captions or subtitles.",
        "0-2 s: the product turns slowly. 2-5 s: the hand lifts it.",
    ],
)
def test_ordinary_words_pass(prompt: str) -> None:
    assert prompt_lint.problems(prompt, person=False) == []


# -----------------------------------------------------------------------------
# The whole catalogue, every profile
# -----------------------------------------------------------------------------
def _people_cases(row: Any) -> list[bool]:
    return {"none": [False], "required": [True], "optional": [False, True]}[row.metadata["person"]]


def test_every_template_compiles_clean_for_every_model(growth: Any, make_png_upload: Any) -> None:
    product = make_product(growth, make_png_upload, photos=2)
    person = avatar_for(growth, make_png_upload)
    profiles = list(VideoModelProfile.objects.all())
    assert profiles, "the profiles are seeded by the catalog fixture"
    failures: list[str] = []
    checked = 0
    for row in ad_templates._rows():
        direction = video_prompt.direction_of(row)
        if row.metadata.get("hidden") or not row.metadata.get("available", True):
            continue
        assert direction is not None, f"{row.key} has no timed direction"
        for with_person in _people_cases(row):
            for location in row.metadata["locations"]:
                render = VideoRender(
                    workspace=growth,
                    product=product,
                    avatar=person if with_person else None,
                    template=row.key,
                    spec={
                        "script": dict(row.metadata.get("script") or {}),
                        "location": location,
                        "creative": {},
                        "prompt": "",
                        "seconds": 10,
                    },
                )
                context = video_prompt.context_for(render, row)
                for profile in profiles:
                    for shot in video_prompt.shots_for(direction, 10):
                        if shot.use not in MOVING_USES:
                            continue  # drawn by the composer, never sent to a model
                        words = video_prompt.prompt_for(context, shot, profile, audio=True)
                        for problem in prompt_lint.problems(words, person=with_person):
                            failures.append(f"{row.key}/{location}/{profile.slug}: {problem}")
                        checked += 1
                if ad_templates.has_frame(row):
                    frame = ad_templates.keyframe_prompt(
                        row,
                        product=product,
                        location=location,
                        person=avatars.describe(person) if with_person else "",
                        workspace=growth,
                    )
                    grounded = prompting.assemble_image_prompt(
                        idea=frame, workspace=growth, product=product
                    ).user
                    for problem in prompt_lint.problems(grounded, person=with_person):
                        failures.append(f"{row.key}/{location}/frame: {problem}")
                    outdoor = all(_outdoor(p) for p in row.metadata["locations"])
                    if outdoor and "window or lamp" in grounded:
                        failures.append(f"{row.key}: a window or lamp lights an outdoor place")
    assert checked > 1000
    assert failures == [], "\n".join(failures[:40])


def _outdoor(place: str) -> bool:
    from ai.models import CreativeKind, CreativeOption

    row = CreativeOption.objects.filter(kind=CreativeKind.PLACE, key=place).first()
    return bool(row and (row.metadata or {}).get("outdoor"))


# -----------------------------------------------------------------------------
# A failing check stops the job before anything is ordered
# -----------------------------------------------------------------------------
def test_a_render_whose_words_contradict_is_refused_unpaid(
    auth_client: Any, growth: Any, make_png_upload: Any, monkeypatch: Any
) -> None:
    from billing.services import ledger

    product = make_product(growth, make_png_upload)
    body = template_clip(product, template="pour-shot")
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    monkeypatch.setattr(prompt_lint, "problems", lambda prompt, person: ["a test contradiction"])
    _fake_video_provider.clear()
    units = ledger.video_balance(growth)
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    assert response.status_code == 202, response.json()
    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.status == "FAILED"
    # The first frame is the first thing checked; a refused frame fails the render.
    assert render.error_code in ("prompt_conflict", "keyframe_failed")
    assert _fake_video_provider.calls == []
    assert ledger.video_balance(growth) == units
    if render.keyframe_id:
        frame = Generation.objects.get(pk=render.keyframe_id)
        assert frame.status == "FAILED"
        assert frame.error_detail["reason"] == "prompt_conflict"
