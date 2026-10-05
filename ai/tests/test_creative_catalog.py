"""The v2 creative catalog (`ai/creative_seed.py`) keeps every contract the
code relies on, and its wording reads correctly where it is placed."""

from __future__ import annotations

import re

import pytest
from django.core.exceptions import ValidationError

from ai.creative_seed import CATALOG, SEED_VERSION
from ai.models import CreativeKind, CreativeOption
from ai.services.creative import MULTI, SINGLE
from ai.video_seed import VIDEO_CATALOG
from products.brief import TONE_KEYS

ROWS = [(kind, row) for kind, rows in {**CATALOG, **VIDEO_CATALOG}.items() for row in rows]


def keys(kind: str) -> set[str]:
    return {row["key"] for row in CATALOG[kind]}


def test_every_kind_is_seeded_and_every_kind_seeded_exists() -> None:
    seeded = {**CATALOG, **VIDEO_CATALOG}
    assert not set(CATALOG) & set(VIDEO_CATALOG)
    assert set(seeded) == {kind.value for kind in CreativeKind}
    assert all(seeded[kind] for kind in seeded)


@pytest.mark.parametrize(("kind", "row"), ROWS, ids=[f"{k}:{r['key']}" for k, r in ROWS])
def test_every_row_is_a_valid_option(kind: str, row: dict) -> None:
    option = CreativeOption(kind=kind, sort_order=0, **row)
    # Fields only (slug key, icon path data, #RRGGBB colours); uniqueness is
    # covered by `test_keys_are_unique_within_a_kind`.
    option.full_clean(exclude=["id"], validate_unique=False, validate_constraints=False)
    assert row["metadata"]["seed"] == SEED_VERSION


def test_keys_are_unique_within_a_kind() -> None:
    for kind, rows in CATALOG.items():
        listed = [row["key"] for row in rows]
        assert len(listed) == len(set(listed)), kind


def test_each_single_choice_control_has_exactly_one_default() -> None:
    for kind in SINGLE:
        defaults = [row["key"] for row in CATALOG[kind] if row["metadata"].get("default")]
        assert len(defaults) == 1, (kind, defaults)
    assert any(row["metadata"].get("default") for row in CATALOG[MULTI["moods"]])


def test_image_fragments_are_phrases_placed_after_a_lead_in() -> None:
    # `ai.services.creative.image_lines` adds "Lead: <fragment>." itself.
    for kind in (
        "scene",
        "light",
        "camera",
        "cast",
        "vibe",
        "palette",
        "dynamics",
        "tempo",
        "mood",
    ):
        for row in CATALOG[kind]:
            fragment = row["prompt_fragment"]
            assert fragment, (kind, row["key"])
            assert not fragment.rstrip().endswith("."), (kind, row["key"])


def test_scene_fragments_read_after_the_picture_is_set() -> None:
    # The copywriter is told "The picture is set <fragment>."
    for row in CATALOG["scene"]:
        assert re.match(r"^(on|in|among|at|under|beside) ", row["prompt_fragment"]), row["key"]


def test_caption_fragments_are_whole_sentences() -> None:
    for kind in ("language", "tone", "cta"):
        for row in CATALOG[kind]:
            assert row["prompt_fragment"].endswith("."), (kind, row["key"])


def test_people_are_directed_toward_realism_and_never_as_endorsers() -> None:
    for row in CATALOG["cast"]:
        if row["key"] == "product-only":
            continue
        assert "natural skin texture" in row["prompt_fragment"], row["key"]
        assert "never presented as a real customer" in row["prompt_fragment"], row["key"]


def test_no_fragment_asks_the_image_model_to_draw_text() -> None:
    for kind in ("scene", "light", "camera", "cast", "vibe", "palette", "mood"):
        for row in CATALOG[kind]:
            assert not re.search(
                r"\b(text|words|lettering|slogan|logo reading)\b", row["prompt_fragment"]
            ), row["key"]


def test_metadata_contracts_the_code_reads() -> None:
    for row in CATALOG["light"]:
        assert {"grade", "overlay"} <= set(row["metadata"]), row["key"]
    for row in CATALOG["palette"]:
        assert row["metadata"].get("ink") and len(row["colors"]) == 2, row["key"]
    for row in CATALOG["cta"]:
        assert row["metadata"].get("short"), row["key"]
    for row in CATALOG["format"]:
        meta = row["metadata"]
        assert meta["kind"] in {"IMAGE", "TEXT"} and re.fullmatch(r"\d+:\d+", meta["aspect"]), row[
            "key"
        ]
        assert meta["post_format"] in {"FEED", "REEL", "STORY"}, row["key"]
    for row in CATALOG["aspect"]:
        assert re.fullmatch(r"\d+:\d+", row["metadata"]["aspect"]), row["key"]
    for row in CATALOG["claim"]:
        assert row["metadata"]["phrases"] and row["description"], row["key"]
    for row in CATALOG["suggestion"]:
        assert row["metadata"]["field"] in {
            "features",
            "use_words",
            "avoid_words",
            "must_include",
            "restrictions",
        }


def test_languages_stay_french_and_english() -> None:
    assert keys("language") == {"fr", "en"}  # L-6


def test_the_toggles_the_code_names_exist() -> None:
    assert {"headline_on_image", "hashtags", "logo_mark"} <= keys("toggle")


def test_voice_presets_set_all_four_sliders_in_range() -> None:
    for row in CATALOG["tone_preset"]:
        values = row["metadata"]["values"]
        assert set(values) == set(TONE_KEYS), row["key"]
        assert all(0 <= v <= 100 for v in values.values()), row["key"]


def test_presets_only_name_choices_that_exist() -> None:
    for row in CATALOG["preset"]:
        for control, value in row["metadata"]["values"].items():
            if control == "moods":
                assert set(value) <= keys("mood"), row["key"]
            else:
                assert value in keys(control), (row["key"], control, value)


@pytest.mark.django_db
def test_a_seeded_row_still_passes_the_models_own_validation_when_saved() -> None:
    for kind, rows in {**CATALOG, **VIDEO_CATALOG}.items():
        for position, row in enumerate(rows):
            CreativeOption.objects.create(kind=kind, sort_order=position, **row)
    assert CreativeOption.objects.count() == len(ROWS)
    with pytest.raises(ValidationError):
        CreativeOption(kind="scene", key="bad", label="x", icon_paths=["<script>"]).full_clean()


def test_every_priced_video_row_carries_a_whole_credit_price() -> None:
    for kind in ("video_length", "motion", "reel_style", "music", "video_extra"):
        for row in VIDEO_CATALOG[kind]:
            credits = row["metadata"]["credits"]
            assert isinstance(credits, int) and credits >= 0, (kind, row["key"])


def test_video_lengths_name_their_mode_seconds_and_render_time() -> None:
    modes = {row["metadata"]["mode"] for row in VIDEO_CATALOG["video_length"]}
    assert modes == {"clip", "reel"}
    for row in VIDEO_CATALOG["video_length"]:
        meta = row["metadata"]
        assert meta["seconds"] > 0 and meta["render_s"] > 0, row["key"]
        if meta["mode"] == "reel":
            assert 1 <= meta["min_shots"] <= meta["max_shots"], row["key"]
    # S3: clips are 5 or 10 seconds; reels are a vertical master.
    assert {
        r["metadata"]["seconds"]
        for r in VIDEO_CATALOG["video_length"]
        if r["metadata"]["mode"] == "clip"
    } == {5, 10}
    assert any(r["metadata"]["aspect"] == "9:16" for r in VIDEO_CATALOG["video_aspect"])


def test_motion_fragments_direct_the_camera_and_never_the_product() -> None:
    for row in VIDEO_CATALOG["motion"]:
        fragment = row["prompt_fragment"]
        assert fragment and not fragment.endswith("."), row["key"]
        assert not re.search(r"\b(text|words|lettering|logo reading)\b", fragment), row["key"]
