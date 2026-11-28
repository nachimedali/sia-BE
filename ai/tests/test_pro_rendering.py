"""S19 Pro rendering on fakes (`upgrade_rendering.md` §13): S19-02 to S19-06.

- S19-02: the compiled prompt per model profile, against goldens; the budget
  trims description, never what happens or what must not;
- S19-03: a rewrite that drops a beat's window is refused; a directed template
  never goes through the rewrite;
- S19-04: every scene's direction is valid, scales to 5, 10 and 15 s keeping
  its hook, and the migration only reaches rows still holding the seed;
- S19-05: each moving shot of a reel is its own call: its own words and seed;
- S19-06: the edits are the composer's, and no scene asks the model for one.
"""

from __future__ import annotations

import importlib
import itertools
import os
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps

from ai.ad_template_seed import DIRECTIONS, TEMPLATES
from ai.models import CreativeKind, CreativeOption, VideoModelProfile, VideoRender
from ai.providers.fake import _fake_text_provider, _fake_video_composer, _fake_video_provider
from ai.route_seed import OMNI_FLASH
from ai.services import rephrase, video_prompt
from ai.services.video_prompt import Beat, compile_parts, scaled
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    RENDERS,
    avatar_for,
    catalog,
    confirmed,
    flag_on,
    growth,
    make_product,
)
from ai.video_profile_seed import FIELDS as PROFILE_FIELDS
from ai.video_profile_seed import PROFILES
from billing.models import FeatureFlag
from billing.services.flags import PRO_RENDERING_S19

pytestmark = pytest.mark.django_db

GOLDENS = Path(__file__).parent / "goldens" / "s19"
SEED = importlib.import_module("ai.migrations.0050_seed_video_profiles_and_pro_routes")
DIRECTIONS_MIGRATION = importlib.import_module("ai.migrations.0051_template_directions")
GOLDEN_PROFILES = {
    "omni": OMNI_FLASH,
    "kling": "kwaivgi/kling-v3.0-pro",
    "grok": "x-ai/grok-imagine-video-1.5-lite",
    "wan": "alibaba/wan-3.0",
}


def golden(name: str, text: str) -> None:
    path = GOLDENS / name
    if os.environ.get("UPDATE_GOLDENS"):
        path.write_text(text)
    assert text == path.read_text(), name


@pytest.fixture(autouse=True)
def clean_fakes() -> Any:
    for fake in (_fake_video_provider, _fake_video_composer):
        fake.clear()
    _fake_text_provider.calls.clear()
    yield


@pytest.fixture
def pro(growth: Any, settings: Any) -> Any:  # noqa: F811
    """Growth with Pro rendering on: the profiles and Pro routes seeded."""
    settings.SEED_CATALOG_ON_MIGRATE = True
    SEED.forwards(apps, None)
    settings.SEED_CATALOG_ON_MIGRATE = False
    FeatureFlag.objects.create(
        organization=growth.organization, key=PRO_RENDERING_S19, enabled=True
    )
    return growth


def _render(client: Any, workspace: Any, upload: Any, key: str, **extra: Any) -> VideoRender:
    product = make_product(
        workspace, upload, photos=extra.pop("photos", 1), kind=extra.pop("kind", "physical")
    )
    body = {
        "mode": "clip",
        "length": extra.pop("length", "clip-10"),
        "template": key,
        "product": product.pk,
        "language": "en",
        **extra,
    }
    if (
        CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key=key).metadata["person"]
        == "required"
        and "avatar" not in body
    ):
        body["avatar"] = avatar_for(workspace, upload).pk
    render = client.post(RENDERS, confirmed(client, body), format="json").json()
    assert render["status"] == "SUCCEEDED", render
    return VideoRender.objects.get(pk=render["id"])


# -----------------------------------------------------------------------------
# S19-02: the compiler
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["pour-shot", "desk-break"])
def test_each_profile_gets_its_own_golden_prompt(
    key: str, auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    render = _render(auth_client, pro, make_png_upload, key)
    template = CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key=key)
    context = video_prompt.context_for(render, template)
    shot = video_prompt.shots_for(template.metadata["direction"], 10)[0]
    specs = {spec["slug"]: spec for spec in PROFILES}
    for name, slug in GOLDEN_PROFILES.items():
        profile = VideoModelProfile(
            slug=slug, **{field: specs[slug][field] for field in PROFILE_FIELDS}
        )
        golden(
            f"{key}-{name}.txt",
            video_prompt.prompt_for(context, shot, profile, audio=profile.audio),
        )
    # What the Growth render actually sent: the Omni prompt.
    assert render.prompt_sent == (GOLDENS / f"{key}-omni.txt").read_text()


def test_the_prompt_runs_in_order_with_timed_beats(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    sent = _render(auth_client, pro, make_png_upload, "pour-shot").prompt_sent

    # S17-43: the frame it keeps comes after the beats; on Omni's budget the
    # place and light lines go first, as the frame holds them.
    labels = ["Camera:", "0-2 s:", "Image 1 is the first frame", "Sound:", "Avoid:"]
    positions = [sent.index(label) for label in labels]
    assert positions == sorted(positions), sent
    assert sent.startswith("Vertical 9:16")
    assert "Avoid: No text, captions or subtitles" in sent
    assert "never changes shape" not in sent.split("Avoid:")[0]  # moved to AVOID
    assert "Timing:" not in sent


def test_over_budget_cuts_style_then_scene_never_action_or_avoid() -> None:
    parts = {
        "format": "Vertical 9:16, close-up",
        "camera": "slow dolly in",
        "action": "0-2 s: the hand enters. 2-6 s: it pours.",
        "scene": "a pale stone counter, a window behind, out of focus, plants on the sill",
        "style": "true-to-life colour, natural texture, deep focus, warm grade, soft grain",
        "avoid": "no text, captions or subtitles; no second bottle",
    }
    full = compile_parts(parts, 500)
    tight = compile_parts(parts, 40)

    assert "Style:" in full and "Setting:" in full
    assert "0-2 s: the hand enters" in tight.lower() or "0-2 s: the hand enters" in tight
    assert "Avoid: No text, captions or subtitles; no second bottle." in tight
    assert len(tight.split()) <= 40 or "Style:" not in tight
    assert "Style:" not in compile_parts(parts, 45)
    assert compile_parts(parts, 10).startswith("Vertical 9:16, close-up. Camera: Slow dolly in.")


@pytest.mark.parametrize(
    ("syntax", "expected"),
    [
        ("seconds", "0-2 s: a. 2-5 s: b."),
        ("brackets", "[0:00-0:02] a. [0:02-0:05] b."),
        ("shots", "Shot 1 (0-2 s): a. Shot 2 (2-5 s): b."),
        ("none", "A. Then B."),
    ],
)
def test_each_timecode_syntax(syntax: str, expected: str) -> None:
    beats = [Beat(0, 2, "a."), Beat(2, 5, "b.")]
    assert video_prompt.timeline(beats, syntax).replace("..", ".") == expected


def test_a_placeholder_with_no_value_never_reaches_the_model_in_braces() -> None:
    assert video_prompt.fill("{product} beside {ingredient_1}", {"product": "Oil"}) == (
        "Oil beside ingredient 1"
    )


# -----------------------------------------------------------------------------
# S19-03: the rewrite keeps the timing
# -----------------------------------------------------------------------------
BRIEF = "Motion: The hand pours. Timing: 0-3 s: Hand enters; 3-7 s: The pour; 7-10 s: Close-up."


def test_a_rewrite_that_flattens_the_beats_is_refused() -> None:
    flat = "A hand gently pours the oil in one smooth, continuous motion as the camera creeps in."
    kept = "0-3 s a hand enters; 3-7 s it pours; 7-10 s a close-up of the label, very slowly."

    assert rephrase.acceptable(flat, BRIEF) is False
    assert rephrase.acceptable(kept, BRIEF) is True
    assert rephrase.acceptable(flat, "Motion: a slow push-in on the product.") is True


def test_with_the_flag_off_a_template_motion_is_never_rewritten_either(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """S17-43: flag off, rewrite on — the template is compiled, not rewritten,
    so a flattening rewrite cannot reach the model at all."""
    from billing.models import FeatureFlag
    from billing.services.flags import STUDIO_REPHRASE

    FeatureFlag.objects.create(organization=None, key=STUDIO_REPHRASE, enabled=True)
    _fake_text_provider.calls.clear()
    render = _render(auth_client, growth, make_png_upload, "pour-shot")

    assert "0-2 s: the hand lifts the product" in render.prompt_sent
    assert not any(
        call["system"].startswith("You write the motion") for call in _fake_text_provider.calls
    )


def test_a_directed_template_is_never_rewritten(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _render(auth_client, pro, make_png_upload, "pour-shot")

    assert not any(
        call["system"].startswith("You write the motion") for call in _fake_text_provider.calls
    )


# -----------------------------------------------------------------------------
# S19-04: timed beats
# -----------------------------------------------------------------------------
def test_every_scene_direction_is_valid_and_lint_free() -> None:
    assert set(DIRECTIONS) >= {spec["key"] for spec in TEMPLATES}
    for key, direction in DIRECTIONS.items():
        assert video_prompt.validate_direction(direction) == [], key
        assert video_prompt.lint(direction) == [], key


def test_a_broken_timeline_or_two_camera_moves_are_refused(catalog: None) -> None:  # noqa: F811
    from django.core.exceptions import ValidationError

    from ai.services import ad_templates

    row = CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key="pour-shot")
    direction = row.metadata["direction"]
    gap = {**direction, "beats": [[0, 2, "a"], [3, 10, "b"]]}
    short = {**direction, "beats": [[0, 2, "a"], [2, 8, "b"]]}
    moves = {**direction, "camera": "a pan right, then a dolly in"}
    for broken in (gap, short, moves):
        with pytest.raises(ValidationError):
            ad_templates.validate_metadata({**row.metadata, "direction": broken})


@pytest.mark.parametrize("seconds", [5, 10, 15])
def test_every_scene_scales_and_keeps_its_hook(seconds: int) -> None:
    for key, direction in DIRECTIONS.items():
        for shot in video_prompt.shots_for(direction, seconds):
            beats = shot.beats
            if not beats:
                continue
            length = shot.end - shot.start
            assert beats[0].start == 0 and beats[-1].end == pytest.approx(length, abs=0.01), key
            assert all(a.end == b.start for a, b in itertools.pairwise(beats)), key
        if not direction.get("shots"):
            first = direction["beats"][0][1]
            assert video_prompt.shots_for(direction, seconds)[0].beats[0].end == min(
                first, seconds / 2
            ), key


def test_scaling_a_ten_second_scene() -> None:
    beats = [[0, 2, "hook"], [2, 6, "pour"], [6, 8, "turn"], [8, 10, "hold"]]
    assert [(b.start, b.end) for b in scaled(beats, 15)] == [
        (0, 2),
        (2, 8.5),
        (8.5, 12),
        (12, 15),
    ]
    assert [(b.start, b.end) for b in scaled(beats, 5)] == [(0, 2), (2, 3.5), (3.5, 4), (4, 5)]


def test_the_migration_reaches_only_rows_still_holding_the_seed(
    catalog: None,  # noqa: F811
    settings: Any,
) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    rows = CreativeOption.objects.filter(
        kind=CreativeKind.AD_TEMPLATE, key__in=[spec["key"] for spec in TEMPLATES]
    )
    for row in rows:
        row.metadata = {k: v for k, v in row.metadata.items() if k != "direction"}
        row.save(update_fields=["metadata"])
    edited = rows.get(key="slow-unbox")
    edited.metadata = {**edited.metadata, "motion_prompt": "Our own motion."}
    edited.save(update_fields=["metadata"])

    DIRECTIONS_MIGRATION.forwards(apps, None)
    DIRECTIONS_MIGRATION.forwards(apps, None)

    edited.refresh_from_db()
    assert "direction" not in edited.metadata
    pour = rows.get(key="pour-shot")
    assert pour.metadata["direction"] == DIRECTIONS["pour-shot"]
    assert rows.exclude(key="slow-unbox").filter(metadata__has_key="direction").count() == (
        rows.count() - 1
    )


# -----------------------------------------------------------------------------
# S19-05: one call per moving shot
# -----------------------------------------------------------------------------
def test_each_moving_shot_has_its_own_words_and_seed(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    render = _render(auth_client, pro, make_png_upload, "reaction-reveal", photos=2)

    calls = _fake_video_provider.calls
    seeds = [request["seed"] for request in _fake_video_provider.requests]
    assert len(calls) == 3 and len(set(calls)) == 3
    assert len(set(seeds)) == 3 and seeds[0] == render.seed
    assert "glances up" in calls[0] and "light gliding across the label" in calls[1]
    assert all(request["model"] == OMNI_FLASH for request in _fake_video_provider.requests)
    [reel] = _fake_video_composer.calls
    assert [shot["clip"] for shot in reel["shots"]] == [True, True, True]
    assert render.prompt_sent == "\n\n".join(calls)


def test_a_scene_that_became_two_shots_renders_as_two(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _render(auth_client, pro, make_png_upload, "trail-test")

    assert len(_fake_video_provider.calls) == 2
    [reel] = _fake_video_composer.calls
    assert reel["shots"][0]["overlay"] != ""
    assert sum(shot["seconds"] for shot in reel["shots"]) == pytest.approx(10 - 2.0)


def test_with_the_flag_off_the_shots_are_compiled_too(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """S17-43: flag off, each moving shot is compiled from its own beats."""
    render = _render(auth_client, growth, make_png_upload, "reaction-reveal", photos=2)

    assert "Image 1 is the first frame" in render.prompt_sent


# -----------------------------------------------------------------------------
# S19-06: the composer's edits
# -----------------------------------------------------------------------------
def test_the_lint_catches_an_edit_asked_of_the_model() -> None:
    direction = {**DIRECTIONS["pour-shot"], "camera": "static, then a punch-in on the label"}
    assert video_prompt.lint(direction)
    beats = [[0, 5, "a hard cut to the product"], [5, 10, "hold"]]
    assert video_prompt.lint({**DIRECTIONS["pour-shot"], "beats": beats})


def test_a_frame_rate_drop_and_a_speed_up_are_the_composers(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _render(auth_client, pro, make_png_upload, "swatch-test", kind="beauty")
    swatch = _fake_video_composer.calls[-1]
    _render(auth_client, pro, make_png_upload, "packing-order")
    packing = _fake_video_composer.calls[-1]

    assert swatch["kind"] == "caption" and swatch["fps"] == 12
    assert packing["speed"] == 1.25 and packing["seconds"] == 10
    # The model was asked for the longer clip the speed-up plays in 10 s.
    assert _fake_video_provider.requests[-1]["model"] == OMNI_FLASH
    assert "stop-motion" not in _fake_video_provider.calls[0]


def test_before_and_after_play_side_by_side(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _render(auth_client, pro, make_png_upload, "before-after", kind="beauty")

    assert len(_fake_video_provider.calls) == 2
    [reel] = _fake_video_composer.calls
    assert len(reel["shots"]) == 1 and reel["shots"][0]["right"] is True


# -----------------------------------------------------------------------------
# S19-07: places, lights, moods and cast vibes
# -----------------------------------------------------------------------------
def test_the_look_lands_in_its_own_part(auth_client: Any, pro: Any, make_png_upload: Any) -> None:
    # S17-43: a phone template keeps its place's light; moods are grade only.
    look = {"moods": ["natural"], "vibe": "everyday"}
    render = _render(
        auth_client, pro, make_png_upload, "desk-break", creative=look, location="office"
    )
    template = CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key="desk-break")
    roomy = VideoModelProfile(slug="roomy", budget_words=500, durations=[10], audio=True)
    shot = video_prompt.shots_for(template.metadata["direction"], 10)[0]
    prompt = video_prompt.prompt_for(
        video_prompt.context_for(render, template), shot, roomy, audio=True
    )

    office = CreativeOption.objects.get(kind=CreativeKind.PLACE, key="office")
    assert f"Light: {office.metadata['light'][:1].upper()}" in prompt
    assert "Style: Shot on a phone's main camera" in prompt
    assert "raw wood, stone and linen" in prompt
    assert "Sound: Quiet room tone and keyboard taps" in prompt
    assert "papers in front, the product on the desk, the window behind" in prompt
    assert "performance: relaxed and unhurried" in prompt
    # The image model's words are untouched.
    warm = CreativeOption.objects.get(kind=CreativeKind.MOOD, key="warm")
    assert "3200K" not in warm.prompt_fragment


def test_a_light_that_fights_the_place_is_refused(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    """S17-43: on a studio template whose light is open, a light that fights the
    place picked is a 400 `not_in_template`; at a place it suits, it goes."""
    from ai.services import ad_templates, template_fit

    template, bad, good, light = next(
        (t, bad, good, key)
        for t in ad_templates._rows()
        if not t.metadata.get("hidden")
        and t.metadata["person"] == "none"
        and set(t.metadata["fits"]) & {"physical"}
        for key in (template_fit.controls(t)["light"].get("open") or [])
        for bad in t.metadata["locations"]
        for good in t.metadata["locations"]
        if bad in template_fit.light_conflicts(key)
        and good not in template_fit.light_conflicts(key)
    )
    product = make_product(pro, make_png_upload)
    body = {
        "mode": "clip",
        "length": template.metadata["lengths"][0],
        "template": template.key,
        "product": product.pk,
        "language": "en",
        "location": bad,
        "creative": {"light": light},
    }
    response = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_in_template"
    assert response.json()["error"]["detail"]["reason"] == "place_light"
    body["location"] = good
    assert auth_client.post("/api/v1/ai/video/estimate/", body, format="json").status_code == 200


def test_with_the_flag_off_the_template_still_decides_the_light(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """S17-43: a phone template keeps its own light, flag on or off."""
    product = make_product(growth, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "slow-unbox",
        "product": product.pk,
        "language": "en",
        "location": "studio",
        "creative": {"light": "golden-hour"},
    }
    response = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["control"] == "light"


def test_the_look_migration_adds_keys_and_keeps_an_operators_own(
    catalog: None,  # noqa: F811
    settings: Any,
) -> None:
    look = importlib.import_module("ai.migrations.0052_video_look")
    settings.SEED_CATALOG_ON_MIGRATE = True
    kitchen = CreativeOption.objects.get(kind=CreativeKind.PLACE, key="kitchen")
    kitchen.metadata = {
        k: v for k, v in kitchen.metadata.items() if k not in ("ambient", "depth")
    } | {"ambient": "our own hum"}
    kitchen.save(update_fields=["metadata"])
    warm = CreativeOption.objects.get(kind=CreativeKind.MOOD, key="warm")
    fragment = warm.prompt_fragment
    warm.metadata = {k: v for k, v in warm.metadata.items() if k not in ("visible", "motion")}
    warm.save(update_fields=["metadata"])

    look.forwards(apps, None)

    kitchen.refresh_from_db()
    warm.refresh_from_db()
    assert kitchen.metadata["ambient"] == "our own hum"
    assert kitchen.metadata["depth"].startswith("the counter edge")
    assert warm.metadata["motion"] == "slow dolly in" and warm.prompt_fragment == fragment


# -----------------------------------------------------------------------------
# S19-09: the capability gate
# -----------------------------------------------------------------------------
def _route_growth_to(slug: str) -> None:
    from ai.models import AIModelRoute

    AIModelRoute.objects.filter(flag=PRO_RENDERING_S19, plan__code="growth").update(
        model=slug, fallback_model=""
    )


def test_every_need_against_every_profile(pro: Any) -> None:
    for spec in PROFILES:
        _route_growth_to(spec["slug"])
        expected = [
            *([] if spec["last_frame"] else ["L", "FL"]),
            *([] if spec["audio"] else ["AU"]),
        ]
        assert video_prompt.missing(pro, ["L", "FL", "AU", "MP"]) == expected, spec["slug"]


def test_a_landing_scene_never_reaches_grok(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _route_growth_to("x-ai/grok-imagine-video-1.5-lite")
    product = make_product(pro, make_png_upload)
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "hero-orbit",
        "product": product.pk,
        "language": "en",
    }
    response = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "route_cannot"
    assert response.json()["error"]["detail"]["needs"] == ["L"]
    assert _fake_video_provider.calls == []


def test_a_landing_scene_renders_on_omni(auth_client: Any, pro: Any, make_png_upload: Any) -> None:
    _render(auth_client, pro, make_png_upload, "hero-orbit")

    [request] = _fake_video_provider.requests
    assert (request["role"], request["model"]) == ("end", OMNI_FLASH)


# -----------------------------------------------------------------------------
# S19-10: native audio
# -----------------------------------------------------------------------------
def test_a_pro_reel_keeps_the_models_sound_under_it(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    _render(auth_client, pro, make_png_upload, "before-after", kind="beauty")

    assert all(request["audio"] is True for request in _fake_video_provider.requests)
    assert all("Sound:" in call for call in _fake_video_provider.calls)
    [reel] = _fake_video_composer.calls
    assert reel["keep_audio"] is True


def test_a_lower_package_asks_for_no_sound(
    auth_client: Any, pro: Any, plans: Any, make_png_upload: Any, settings: Any
) -> None:
    from ai.tests.test_ad_templates import on_plan

    settings.SEED_CATALOG_ON_MIGRATE = True
    importlib.import_module("ai.migrations.0021_seed_model_routes").forwards(apps, None)
    settings.SEED_CATALOG_ON_MIGRATE = False
    on_plan(pro, plans, "essential")
    render = _render(auth_client, pro, make_png_upload, "pour-shot")

    [request] = _fake_video_provider.requests
    assert request["audio"] is None and request["model"] == "x-ai/grok-imagine-video-1.5-lite"
    assert "Sound:" not in render.prompt_sent and "0-2 s:" in render.prompt_sent


# -----------------------------------------------------------------------------
# S19-11: draft, then final
# -----------------------------------------------------------------------------
def _first_frame(workspace: Any, upload: Any) -> Any:
    from content.services.media import ingest_media

    return ingest_media(workspace=workspace, upload=upload("frame.png", (720, 1280)))


def _draft_body(product: Any, frame: Any, **extra: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": "clip-10",
        "template": "pour-shot",
        "product": product.pk,
        "language": "en",
        "source": frame.pk,
        **extra,
    }


def test_a_draft_costs_a_reroll_and_its_final_one_unit_at_the_same_seed(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    from billing.models import VideoLedger

    product = make_product(pro, make_png_upload)
    frame = _first_frame(pro, make_png_upload)
    units_before = VideoLedger.objects.filter(workspace=pro).count()
    draft = auth_client.post(
        RENDERS, confirmed(auth_client, _draft_body(product, frame, draft=True)), format="json"
    ).json()

    assert draft["status"] == "SUCCEEDED", draft
    assert (draft["units"], draft["charged_units"], draft["charged"]) == (0, 0, 0)
    assert VideoLedger.objects.filter(workspace=pro).count() == units_before
    assert _fake_video_provider.requests[-1]["resolution"] == "720p"

    final = auth_client.post(
        RENDERS,
        confirmed(auth_client, _draft_body(product, frame, final_of=draft["id"])),
        format="json",
    ).json()

    assert final["status"] == "SUCCEEDED", final
    assert final["charged_units"] == 1
    assert (
        VideoRender.objects.get(pk=final["id"]).seed == VideoRender.objects.get(pk=draft["id"]).seed
    )
    assert _fake_video_provider.requests[-1]["resolution"] == "1080p"
    assert VideoLedger.objects.filter(workspace=pro).count() == units_before + 1


def test_a_failed_draft_uses_no_reroll_and_the_last_one_is_a_402(
    auth_client: Any, pro: Any, plans: Any, make_png_upload: Any, monkeypatch: Any
) -> None:
    from common.exceptions import ProviderError

    growth_plan = plans["growth"]
    growth_plan.features = {**growth_plan.features, "ad_rerolls": 1}
    growth_plan.save()
    product = make_product(pro, make_png_upload)
    frame = _first_frame(pro, make_png_upload)
    body = _draft_body(product, frame, draft=True)

    real = _fake_video_provider.animate
    monkeypatch.setattr(
        _fake_video_provider,
        "animate",
        lambda **kwargs: (_ for _ in ()).throw(ProviderError("down", detail={"status": 500})),
    )
    failed = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()
    assert failed["status"] == "FAILED"
    monkeypatch.setattr(_fake_video_provider, "animate", real)

    ok = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()
    assert ok["status"] == "SUCCEEDED", ok
    refused = auth_client.post(RENDERS, confirmed(auth_client, body), format="json")

    assert refused.status_code == 402
    error = refused.json()["error"]
    assert error["code"] == "reroll_limit_reached" and error["upgrade"]
    assert error["detail"] == {"feature": "ad_rerolls", "limit": 1, "used": 1}


def test_a_draft_needs_a_seeded_model_a_frame_and_the_flag(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    product = make_product(pro, make_png_upload)
    frame = _first_frame(pro, make_png_upload)
    estimate = "/api/v1/ai/video/estimate/"

    no_frame = _draft_body(product, frame, draft=True)
    del no_frame["source"]
    assert auth_client.post(estimate, no_frame, format="json").status_code == 400

    _route_growth_to("kwaivgi/kling-v3.0-pro")
    no_seed = auth_client.post(estimate, _draft_body(product, frame, draft=True), format="json")
    assert no_seed.status_code == 422 and no_seed.json()["error"]["detail"]["needs"] == ["seed"]

    FeatureFlag.objects.filter(key=PRO_RENDERING_S19).update(enabled=False)
    off = auth_client.post(estimate, _draft_body(product, frame, draft=True), format="json")
    assert off.status_code == 400


def test_a_final_of_another_organizations_or_brands_draft_is_a_404(
    auth_client: Any, pro: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

    product = make_product(pro, make_png_upload)
    frame = _first_frame(pro, make_png_upload)
    here = {"HTTP_X_WORKSPACE_ID": str(pro.pk)}
    for elsewhere in (
        provision_workspace(other_user, name="Stranger Co"),
        provision_extra_workspace(user=user, name="Second brand"),
    ):
        theirs = VideoRender.objects.create(
            workspace=elsewhere,
            created_by=user,
            mode="clip",
            spec={"draft": True, "template": "pour-shot", "source": frame.pk, "length": "clip-10"},
            estimate_id="est_x",
            credits=0,
            status="SUCCEEDED",
            seed=7,
        )
        response = auth_client.post(
            "/api/v1/ai/video/estimate/",
            _draft_body(product, frame, final_of=theirs.pk),
            format="json",
            **here,
        )
        assert response.status_code == 404, elsewhere


# -----------------------------------------------------------------------------
# S19-12: the 26 new scenes
# -----------------------------------------------------------------------------
def _ready_for_every_scene(workspace: Any, upload: Any) -> Any:
    """A product that meets every new scene's needs: a confirmed card listing
    its parts, a proven claim, three variant photos, two of the brand's own
    place photos."""
    from datetime import timedelta

    from django.utils import timezone

    from brand.models import BrandPlace, PlaceSource
    from content.services.media import ingest_media

    product = make_product(workspace, upload, photos=3)
    product.identity = {
        "shape": "a tall amber bottle",
        "colours": "amber, black cap",
        "label": ["OLIO"],
        "keep": "the black cap",
        "parts": ["olives", "lemon", "thyme"],
    }
    product.identity_confirmed_at = timezone.now()
    proof = ingest_media(workspace=workspace, upload=upload("proof.png", (400, 400)))
    product.claims = [{"key": "cold-pressed", "proof_media": proof.pk}]
    product.save(update_fields=["identity", "identity_confirmed_at", "claims"])
    for name in ("before", "after"):
        BrandPlace.objects.create(
            workspace=workspace,
            label=name,
            source=PlaceSource.UPLOAD,
            media=ingest_media(workspace=workspace, upload=upload(f"{name}.png", (500, 800))),
        )
    product.offer = {"text": "Launching soon", "ends": str(timezone.localdate() + timedelta(9))}
    return product


def test_every_new_scene_has_french_and_italian(catalog: None) -> None:  # noqa: F811
    from ai.scene_seed_s19 import KEYS

    assert len(KEYS) == 26
    for row in CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key__in=KEYS):
        assert row.translations["fr"]["label"] and row.translations["it"]["label"], row.key
        assert row.metadata["premium"] is True and row.metadata["pro"] is True


@pytest.mark.parametrize("key", __import__("ai.scene_seed_s19").scene_seed_s19.KEYS)
def test_each_new_scene_renders_on_growth(
    key: str, auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    from ai.scene_seed_s19 import SCENES

    spec = next(row for row in SCENES if row["key"] == key)["metadata"]
    product = _ready_for_every_scene(pro, make_png_upload)
    kind = "service" if "service" in spec["fits"] and "physical" not in spec["fits"] else ""
    if kind:
        product.offer_kind = kind
        product.save(update_fields=["offer_kind"])
    body: dict[str, Any] = {
        "mode": "clip",
        "length": spec["lengths"][-1],
        "template": key,
        "product": product.pk,
        "language": "en",
    }
    if spec["person"] == "required":
        body["avatar"] = avatar_for(pro, make_png_upload).pk
    if spec["needs"] == "offer":
        body["offer"] = product.offer
    response = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")
    if not spec["available"]:
        assert response.status_code == 409, response.json()
        return
    assert response.status_code == 200, response.json()
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()

    assert render["status"] == "SUCCEEDED", render
    assert _fake_video_provider.calls, key
    assert not any("{" in call for call in _fake_video_provider.calls), key
    if spec["direction"]["route"] == "FL":
        assert _fake_video_provider.requests[-1].get("last_image") is True
        assert render["keyframe"] is None


def test_new_scenes_are_premium_on_essential_and_absent_with_the_flag_off(
    auth_client: Any, pro: Any, plans: Any, make_png_upload: Any
) -> None:
    from ai.tests.test_ad_templates import on_plan

    product = make_product(pro, make_png_upload)
    on_plan(pro, plans, "essential")
    body = {
        "mode": "clip",
        "length": "clip-10",
        "template": "light-sweep",
        "product": product.pk,
        "language": "en",
    }
    locked = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")
    assert locked.status_code == 402 and locked.json()["error"]["upgrade"]

    FeatureFlag.objects.filter(key=PRO_RENDERING_S19).update(enabled=False)
    gone = auth_client.post("/api/v1/ai/video/estimate/", body, format="json")
    assert gone.status_code == 400
    keys = {
        row["key"]
        for row in auth_client.get(f"/api/v1/ai/ad-templates/?product={product.pk}").json()[
            "templates"
        ]
    }
    assert "light-sweep" not in keys and "pour-shot" in keys


@pytest.mark.usefixtures("all_templates")
def test_a_new_scene_is_shown_but_does_not_fit_without_what_it_shows(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    product = make_product(pro, make_png_upload)
    rows = {
        row["key"]: row
        for row in auth_client.get(f"/api/v1/ai/ad-templates/?product={product.pk}").json()[
            "templates"
        ]
    }

    assert rows["ingredient-orbit"]["fit"] == "needs_parts"
    assert rows["put-to-test"]["fit"] == "needs_claims"
    assert rows["light-sweep"]["fit"] is None
    assert rows["colour-morph"]["frame"] is False  # two real photos, no drawn frame


# -----------------------------------------------------------------------------
# S19-13: beat frames at review
# -----------------------------------------------------------------------------
def test_a_pro_render_keeps_a_still_at_the_middle_of_each_beat(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    render = _render(auth_client, pro, make_png_upload, "pour-shot")
    payload = auth_client.get(f"{RENDERS}{render.pk}/").json()

    assert [frame["at"] for frame in payload["beat_frames"]] == [1.0, 4.0, 7.0, 9.0]
    assert all(frame["url"] for frame in payload["beat_frames"])
    assert "pours into the bowl" in payload["beat_frames"][1]["label"]
    reviewed = auth_client.patch(f"{RENDERS}{render.pk}/", {"review": "ACCEPTED"}, format="json")
    assert reviewed.status_code == 200, reviewed.json()


def test_a_reels_frames_sit_on_each_shot_before_the_end_card(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    render = _render(auth_client, pro, make_png_upload, "reaction-reveal", photos=2)

    marks = [frame["at"] for frame in render.beat_frames]
    assert len(marks) == 3 and marks == sorted(marks) and marks[-1] < 10 - 2.0
    assert _fake_video_composer.frame_requests == [marks]


def test_every_template_render_has_its_beat_stills(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """S17-43: the reviewer checks every template clip against its beats, Pro or
    not — no machine check can tell a lost scene from a camera move."""
    render = _render(auth_client, growth, make_png_upload, "pour-shot")
    assert render.beat_frames != []


def test_a_composer_that_cannot_make_frames_costs_the_frames_only(
    auth_client: Any, pro: Any, make_png_upload: Any, monkeypatch: Any
) -> None:
    from common.exceptions import ProviderError

    def broken(**kwargs: Any) -> Any:
        raise ProviderError("no frames")

    monkeypatch.setattr(_fake_video_composer, "frames", broken)
    render = _render(auth_client, pro, make_png_upload, "pour-shot")

    assert render.status == "SUCCEEDED" and render.beat_frames == []


# -----------------------------------------------------------------------------
# S19-14 (server side): the gallery says what is Pro and what cannot be made
# -----------------------------------------------------------------------------
@pytest.mark.usefixtures("all_templates")
def test_the_gallery_marks_pro_scenes_and_what_the_model_cannot_do(
    auth_client: Any, pro: Any, make_png_upload: Any
) -> None:
    product = make_product(pro, make_png_upload)
    gallery = f"/api/v1/ai/ad-templates/?product={product.pk}"
    rows = {row["key"]: row for row in auth_client.get(gallery).json()["templates"]}
    assert rows["light-sweep"]["pro"] is True and rows["pour-shot"]["pro"] is False
    assert rows["light-sweep"]["cannot"] == [] and rows["texture-sound"]["cannot"] == []

    # Grok Imagine 1.5 (not Lite, which makes sound since S17-38): no last frame, no sound.
    _route_growth_to("x-ai/grok-imagine-video-1.5")
    rows = {row["key"]: row for row in auth_client.get(gallery).json()["templates"]}
    assert rows["light-sweep"]["cannot"] == ["L"]
    assert rows["texture-sound"]["cannot"] == ["AU"]
