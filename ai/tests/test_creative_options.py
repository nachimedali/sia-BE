"""The Studio's creative controls as data.

Scenes, lights, camera angles, who is in the frame, moods, palettes, languages,
calls to action, formats, the three sliders, the toggles, the presets and the
brief's quick tags are **rows** (`CreativeOption`), not constants in the
frontend. Adding a scene next quarter is a row in admin; nothing here ships.

What these tests pin:

* the catalog the Studio renders itself from, and that it is seeded
  idempotently and readable only when signed in;
* that a selection is **validated against the catalog** — an unknown, inactive
  or mistyped choice is a 400, never a silently ignored field, because a
  control that appears to work and changes nothing is the worst kind of bug;
* that every choice **reaches the generation** — it is stored on the row, it
  is in the prompt, and the image provider is told how to grade — so "capture
  everything in the backend" is asserted rather than hoped.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.urls import reverse
from PIL import Image
from rest_framework.exceptions import ValidationError

from ai.models import CreativeKind, CreativeOption, GenerationKind, GenerationMode
from ai.providers.fake import _fake_image_provider, _fake_text_provider
from ai.services import creative
from ai.services.pipeline import create_generation, run_generation
from billing.services.ledger import grant_credits

pytestmark = pytest.mark.django_db

URL = "/api/v1/ai/creative-options/"
GENERATE_URL = "/api/v1/ai/generate/"

PRODUCT_FORM_KINDS = {"audience", "tone_preset", "claim", "shot_tag", "aspect", "suggestion"}

#: Every kind the template's controls need. A kind with no rows is a control
#: with nothing in it, which the Studio would render as an empty box.
ALL_KINDS = {
    "scene",
    "light",
    "camera",
    "cast",
    "vibe",
    "mood",
    "palette",
    "language",
    "cta",
    "format",
    "tempo",
    "dynamics",
    "tone",
    "toggle",
    "preset",
    "quick_tag",
    # The product form's lists: chips with text, no icon to draw.
    *PRODUCT_FORM_KINDS,
}


@pytest.fixture
def seeded() -> None:
    call_command("seed_creative_options", verbosity=0)


@pytest.fixture
def studio_workspace(workspace: Any, plans: Any, generation_costs: None) -> Any:
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan", "updated_at"])
    grant_credits(workspace, 500, note="test funding")
    return workspace


FULL = {
    "scene": "djerba",
    "light": "golden",
    "camera": "low",
    "cast": "hands",
    "vibe": "casual",
    "moods": ["editorial", "warm"],
    "palette": "sunset",
    "language": "fr",
    "cta": "shop",
    "format": "single",
    "tempo": "allegro",
    "dynamics": "forte",
    "tone": "warm",
    "toggles": {"headline_on_image": True, "hashtags": True, "logo_mark": False},
    "avoid": "plastic, stock logos",
    "platforms": ["instagram", "facebook"],
}


# -----------------------------------------------------------------------------
# The catalog
# -----------------------------------------------------------------------------
def test_the_seed_covers_every_control_and_is_idempotent(seeded: None) -> None:
    kinds = set(CreativeOption.objects.values_list("kind", flat=True))
    assert kinds == ALL_KINDS

    before = CreativeOption.objects.count()
    call_command("seed_creative_options", verbosity=0)
    assert CreativeOption.objects.count() == before


def test_the_template_scenes_and_the_four_camera_angles_are_there(seeded: None) -> None:
    scenes = CreativeOption.objects.filter(kind=CreativeKind.SCENE).values_list("key", flat=True)
    assert set(scenes) == {"sidibou", "souk", "studio", "djerba", "dunes", "rooftop"}
    cameras = CreativeOption.objects.filter(kind=CreativeKind.CAMERA).values_list("key", flat=True)
    assert list(cameras) == ["eye", "top", "low", "macro"]
    cast = CreativeOption.objects.filter(kind=CreativeKind.CAST).values_list("key", flat=True)
    assert list(cast) == ["none", "hands", "one", "group"]


def test_content_languages_are_french_and_english_only(seeded: None) -> None:
    """L-6. A third language is a change request, not a row."""
    codes = CreativeOption.objects.filter(kind=CreativeKind.LANGUAGE).values_list("key", flat=True)
    assert set(codes) == {"fr", "en"}


def test_every_row_has_text_and_an_icon_to_draw(seeded: None) -> None:
    """The brief asks for icons *and* text on every control."""
    bare = [
        f"{row.kind}/{row.key}"
        for row in CreativeOption.objects.exclude(kind=CreativeKind.QUICK_TAG).exclude(
            kind__in=PRODUCT_FORM_KINDS
        )
        if not row.label or not row.icon_paths
    ]
    assert bare == []


def test_presets_only_name_options_that_exist(seeded: None) -> None:
    """A preset pointing at a deleted scene would apply half a recipe."""
    catalog = {(row.kind, row.key) for row in CreativeOption.objects.all()}
    for preset in CreativeOption.objects.filter(kind=CreativeKind.PRESET):
        values = preset.metadata["values"]
        for kind in ("scene", "light", "camera", "cast", "vibe", "tempo"):
            if kind in values:
                assert (kind, values[kind]) in catalog, f"{preset.key}: {kind}={values[kind]}"
        for mood in values.get("moods", []):
            assert ("mood", mood) in catalog


def test_the_catalog_endpoint_groups_orders_and_hides_inactive(
    auth_client: Any, workspace: Any, seeded: None, generation_costs: None
) -> None:
    CreativeOption.objects.filter(kind="scene", key="dunes").update(is_active=False)
    CreativeOption.objects.filter(kind="scene", key="souk").update(sort_order=-5)

    body = auth_client.get(URL).json()

    assert set(body["options"]) == ALL_KINDS
    scenes = [row["key"] for row in body["options"]["scene"]]
    assert "dunes" not in scenes
    assert scenes[0] == "souk"
    first = body["options"]["camera"][0]
    assert set(first) >= {"key", "label", "description", "icon_paths", "colors", "metadata"}
    assert first["label"] == "Eye level"


def test_the_catalog_carries_the_prices_the_estimate_reads(
    auth_client: Any, workspace: Any, seeded: None, generation_costs: None
) -> None:
    """Rule 10: no commercial number is hardcoded, so the Studio's "≈ N
    credits" has to come from the cost table and not from the browser."""
    body = auth_client.get(URL).json()
    assert body["pricing"]["IMAGE"]["credits"] > 0
    assert body["pricing"]["TEXT"]["credits"] > 0
    assert body["pricing"]["IMAGE"]["variant_pool"] >= 1


def test_the_catalog_needs_a_signed_in_user(client: Any, seeded: None) -> None:
    assert client.get(URL).status_code in (401, 403)


def test_the_catalog_is_read_only_over_the_api(auth_client: Any, seeded: None) -> None:
    assert auth_client.post(URL, {}, format="json").status_code == 405


def test_an_icon_that_is_not_path_data_is_refused(seeded: None) -> None:
    """Icons are drawn as `<path d>` from the database, so the value must be
    path data and nothing that could carry markup."""
    row = CreativeOption(
        kind=CreativeKind.SCENE,
        key="evil",
        label="Evil",
        icon_paths=["<script>alert(1)</script>"],
    )
    with pytest.raises(DjangoValidationError):
        row.full_clean()


def test_colours_must_be_hex(seeded: None) -> None:
    row = CreativeOption(
        kind=CreativeKind.SCENE, key="bad", label="Bad", icon_paths=["M0 0"], colors=["red; x:y"]
    )
    with pytest.raises(DjangoValidationError):
        row.full_clean()


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def test_a_full_selection_normalises_to_itself(seeded: None) -> None:
    assert creative.normalize(FULL) == FULL


def test_an_empty_selection_is_allowed_and_stays_empty(seeded: None) -> None:
    """Every non-Studio caller sends nothing, and must be priced and rendered
    exactly as before."""
    assert creative.normalize({}) == {}
    assert creative.normalize(None) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"scene": "atlantis"},
        {"light": "golden hour"},  # a label, not a key
        {"moods": ["editorial", "nope"]},
        {"moods": "editorial"},  # must be a list
        {"toggles": {"confetti": True}},
        {"toggles": {"hashtags": "yes"}},
        {"platforms": ["myspace"]},
        {"avoid": "x" * 201},
        {"warp_speed": 9},
        {"scene": ["sidibou"]},
    ],
)
def test_a_choice_the_catalog_does_not_hold_is_refused(seeded: None, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        creative.normalize(bad)


def test_an_inactive_option_cannot_be_chosen(seeded: None) -> None:
    CreativeOption.objects.filter(kind="scene", key="dunes").update(is_active=False)
    with pytest.raises(ValidationError):
        creative.normalize({"scene": "dunes"})


def test_a_new_row_is_immediately_choosable(seeded: None) -> None:
    """The point of the whole model: no deploy to add a scene."""
    CreativeOption.objects.create(
        kind=CreativeKind.SCENE,
        key="harbour",
        label="Harbour at dusk",
        icon_paths=["M3 18h18"],
        prompt_fragment="on a quiet harbour quay at dusk",
    )
    assert creative.normalize({"scene": "harbour"}) == {"scene": "harbour"}
    assert "on a quiet harbour quay at dusk" in " ".join(creative.image_lines({"scene": "harbour"}))


# -----------------------------------------------------------------------------
# The request
# -----------------------------------------------------------------------------
def _generate(auth_client: Any, **extra: Any) -> Any:
    return auth_client.post(
        GENERATE_URL,
        {"kind": "TEXT", "mode": "IDEA", "prompt": "a cosy morning", "n": 1, **extra},
        format="json",
    )


def test_the_choices_are_stored_on_the_generation(
    auth_client: Any, studio_workspace: Any, seeded: None
) -> None:
    response = _generate(auth_client, kind="IMAGE", creative=FULL)
    assert response.status_code == 201
    body = response.json()
    assert body["creative"] == FULL
    # The format owns the aspect: "single" is 4:5 whatever else was sent.
    assert body["aspect"] == "4:5"
    # The legacy columns stay readable for everything that predates the brief.
    assert body["scene"] == "Djerba shoreline"
    assert body["render_style"] == "Editorial, Warm"


def test_a_format_that_disagrees_with_the_kind_is_refused(
    auth_client: Any, studio_workspace: Any, seeded: None
) -> None:
    """A "Reel 9:16" that quietly produced a text post would be a control that
    changed nothing."""
    response = _generate(auth_client, kind="TEXT", creative={"format": "story"})
    assert response.status_code == 400
    assert "IMAGE" in str(response.json())


def test_a_request_with_no_creative_is_unchanged(
    auth_client: Any, studio_workspace: Any, seeded: None
) -> None:
    response = _generate(auth_client)
    assert response.status_code == 201
    assert response.json()["creative"] == {}


def test_an_unknown_choice_is_a_400_naming_the_field(
    auth_client: Any, studio_workspace: Any, seeded: None
) -> None:
    response = _generate(auth_client, creative={"scene": "atlantis"})
    assert response.status_code == 400
    assert "creative" in str(response.json())


# -----------------------------------------------------------------------------
# Reaching the model
# -----------------------------------------------------------------------------
def test_every_choice_is_in_the_image_prompt(seeded: None) -> None:
    lines = " ".join(creative.image_lines(FULL))
    for fragment in (
        CreativeOption.objects.get(kind="scene", key="djerba").prompt_fragment,
        CreativeOption.objects.get(kind="light", key="golden").prompt_fragment,
        CreativeOption.objects.get(kind="camera", key="low").prompt_fragment,
        CreativeOption.objects.get(kind="cast", key="hands").prompt_fragment,
        CreativeOption.objects.get(kind="palette", key="sunset").prompt_fragment,
        CreativeOption.objects.get(kind="mood", key="editorial").prompt_fragment,
        CreativeOption.objects.get(kind="mood", key="warm").prompt_fragment,
    ):
        assert fragment and fragment in lines
    assert "plastic, stock logos" in lines


def test_the_brief_for_the_copy_carries_language_tone_and_the_call_to_action(
    seeded: None,
) -> None:
    lines = " ".join(creative.caption_lines(FULL))
    assert "French" in lines
    assert CreativeOption.objects.get(kind="tone", key="warm").prompt_fragment in lines
    assert CreativeOption.objects.get(kind="cta", key="shop").prompt_fragment in lines


def test_a_run_hands_the_provider_the_prompt_and_the_grade(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> None:
    row = create_generation(
        workspace=studio_workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug on a sunlit table",
        product=product_with_reference_image,
        creative=creative.normalize(FULL),
    )
    run_generation(row, n=2)

    call = _fake_image_provider.calls[-1]
    assert "Djerba" in call["prompt"] or "shoreline" in call["prompt"].lower()
    assert call["style"] is not None
    assert set(call["style"]) >= {"chroma", "cb", "cr"}


def _mean_chroma(content: bytes) -> tuple[float, float]:
    _y, cb, cr = Image.open(io.BytesIO(content)).convert("YCbCr").split()

    def mean(channel: Image.Image) -> float:
        data = list(channel.getdata())
        return sum(data) / len(data)

    return mean(cb), mean(cr)


def test_golden_and_blue_hour_render_differently_and_both_still_pass_the_gate(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> None:
    """The fake's mock-up honours the light, in chroma only — so the variants
    are visibly graded by what was chosen while the identity check, which reads
    luma, scores them as the product."""
    means = {}
    for light in ("golden", "blue"):
        row = create_generation(
            workspace=studio_workspace,
            user=user,
            kind=GenerationKind.IMAGE,
            mode=GenerationMode.PRODUCT,
            prompt="the mug",
            product=product_with_reference_image,
            creative=creative.normalize({"light": light}),
        )
        done = run_generation(row, n=1)
        assert done.status == "SUCCEEDED", done.error_detail
        variant = done.variants.first()
        assert variant is not None and variant.media_asset is not None
        with variant.media_asset.file.open("rb") as handle:
            means[light] = _mean_chroma(handle.read())

    assert means["golden"] != means["blue"]
    # Warm pushes red up and blue down relative to cool.
    assert means["golden"][1] > means["blue"][1]
    assert means["golden"][0] < means["blue"][0]


# -----------------------------------------------------------------------------
# What a variant carries
# -----------------------------------------------------------------------------
def test_a_studio_image_variant_carries_a_headline_a_caption_and_its_match(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> None:
    row = create_generation(
        workspace=studio_workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug on a sunlit table",
        product=product_with_reference_image,
        creative=creative.normalize(FULL),
    )
    done = run_generation(row, n=2)

    variants = list(done.variants.all())
    assert len(variants) == 2
    for variant in variants:
        assert variant.media_asset is not None
        assert variant.headline.strip()
        assert variant.body.strip()
        assert variant.body != variant.headline
        # The gate's own similarity score, kept on the row: a "match" figure
        # with a source, not a decoration.
        assert variant.identity_score is not None
        assert 0.6 <= variant.identity_score <= 1.0

    # The copy was asked for in the chosen language, through the text port.
    asked = _fake_text_provider.calls[-1]
    assert "French" in asked["system"] + asked["prompt"]


def test_an_image_run_with_no_creative_stays_image_only(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> None:
    """Autopilot and every other non-Studio caller: no copy call, no extra
    provider spend, rows exactly as before."""
    row = create_generation(
        workspace=studio_workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug",
        product=product_with_reference_image,
    )
    done = run_generation(row, n=1)
    variant = done.variants.first()
    assert variant is not None
    assert variant.body == "" and variant.headline == ""
    assert _fake_text_provider.calls == []


def test_hashtags_follow_the_toggle(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> None:
    for on in (False, True):
        row = create_generation(
            workspace=studio_workspace,
            user=user,
            kind=GenerationKind.IMAGE,
            mode=GenerationMode.PRODUCT,
            prompt="the mug",
            product=product_with_reference_image,
            creative=creative.normalize({"toggles": {"hashtags": on}}),
        )
        variant = run_generation(row, n=1).variants.first()
        assert variant is not None
        if not on:
            assert variant.hashtags == []
        else:
            assert isinstance(variant.hashtags, list)


# -----------------------------------------------------------------------------
# Editing and committing
# -----------------------------------------------------------------------------
@pytest.fixture
def finished(
    studio_workspace: Any, user: Any, product_with_reference_image: Any, seeded: None
) -> Any:
    row = create_generation(
        workspace=studio_workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug",
        product=product_with_reference_image,
        creative=creative.normalize(FULL),
        paid_slots=2,
    )
    return run_generation(row, n=2)


def test_a_variants_copy_can_be_edited_before_it_is_sent(auth_client: Any, finished: Any) -> None:
    variant = finished.variants.first()
    response = auth_client.post(
        reverse("generation-edit-variant", args=[finished.pk]),
        {"variant": variant.pk, "headline": "Le goût de la saison.", "body": "Notre huile."},
        format="json",
    )
    assert response.status_code == 200
    variant.refresh_from_db()
    assert variant.headline == "Le goût de la saison."
    assert variant.body == "Notre huile."


def test_an_edit_may_change_one_field_and_leave_the_other(auth_client: Any, finished: Any) -> None:
    variant = finished.variants.first()
    body = variant.body
    auth_client.post(
        reverse("generation-edit-variant", args=[finished.pk]),
        {"variant": variant.pk, "headline": "Only this"},
        format="json",
    )
    variant.refresh_from_db()
    assert variant.headline == "Only this" and variant.body == body


def test_editing_a_variant_of_another_generation_is_a_404(
    auth_client: Any, finished: Any, studio_workspace: Any, user: Any
) -> None:
    other = create_generation(
        workspace=studio_workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="x",
    )
    stranger = run_generation(other, n=1).variants.first()
    assert stranger is not None
    response = auth_client.post(
        reverse("generation-edit-variant", args=[finished.pk]),
        {"variant": stranger.pk, "headline": "x"},
        format="json",
    )
    assert response.status_code == 404


def test_editing_across_workspaces_is_a_404(
    finished: Any, other_user: Any, plans: Any, client: Any
) -> None:
    from rest_framework.test import APIClient

    from workspaces.services.provisioning import provision_workspace

    provision_workspace(other_user, name="Elsewhere")
    stranger = APIClient()
    stranger.force_authenticate(other_user)
    variant = finished.variants.first()
    response = stranger.post(
        reverse("generation-edit-variant", args=[finished.pk]),
        {"variant": variant.pk, "headline": "x"},
        format="json",
    )
    assert response.status_code == 404


def test_a_committed_image_variant_becomes_a_draft_with_its_caption(
    finished: Any, user: Any
) -> None:
    from ai.services import variants as variant_service

    chosen = finished.variants.first()
    variant_service.select(finished, variant_ids=[chosen.pk], actor=user)
    [post] = variant_service.commit(finished, actor=user)

    assert chosen.body in post.master_body
    assert post.media_attachments.count() == 1
    assert post.status == "DRAFT"


def test_a_committed_caption_carries_the_hashtags_the_user_asked_for(
    finished: Any, user: Any
) -> None:
    from ai.services import variants as variant_service

    chosen = finished.variants.first()
    chosen.hashtags = ["#ceramics", "#madeintunisia"]
    chosen.save(update_fields=["hashtags"])
    variant_service.select(finished, variant_ids=[chosen.pk], actor=user)
    [post] = variant_service.commit(finished, actor=user)

    assert "#ceramics #madeintunisia" in post.master_body
