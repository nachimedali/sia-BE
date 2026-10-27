"""S17 — ad templates, keyframes, video units, reference caps and the AI label
(`ads_templates.md`, `avatar_upgrade/analysis.md`).

Every plan lock is checked at the server; every error code the endpoints emit
has a test; another organization's and another brand's ids are 404; the flag
off is the Studio as it was.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command

from ai.ad_template_seed import TEMPLATES
from ai.models import CreativeKind, CreativeOption, Generation, VideoRender, VideoRenderStatus
from ai.providers.fake import _fake_video_provider
from ai.services import ad_templates, disclosure, video
from ai.services.pipeline import _reference_image_bytes
from billing.models import CreditLedger, FeatureFlag, VideoLedger
from billing.services import ledger
from billing.services.flags import AD_TEMPLATES_S17, CHECKS_S4
from brand.models import Avatar, AvatarKind
from checks.models import CheckStatus
from common.exceptions import ProviderError
from content.services.media import ingest_media
from products.services.products import attach_reference_images, create_product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

GALLERY = "/api/v1/ai/ad-templates/"
KEYFRAME = "/api/v1/ai/ad-templates/keyframe/"
ESTIMATE = "/api/v1/ai/video/estimate/"
RENDERS = "/api/v1/ai/video/renders/"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def catalog(db: None, generation_costs: None) -> None:
    call_command("seed_creative_options", verbosity=0)


@pytest.fixture
def flag_on(db: None) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=True)


def on_plan(workspace: Any, plans: dict[str, Any], code: str) -> Any:
    workspace.organization.plan = plans[code]
    workspace.organization.save(update_fields=["plan"])
    return workspace


@pytest.fixture
def growth(workspace: Any, plans: dict[str, Any], catalog: None, flag_on: None) -> Any:
    on_plan(workspace, plans, "growth")
    ledger.grant_credits(workspace, 200, note="test")
    ledger.grant_video_units(workspace, 9, note="test")
    return workspace


def make_product(workspace: Any, upload: Any, *, kind: str = "physical", photos: int = 1) -> Any:
    product = create_product(workspace=workspace, name="Olive oil 500 ml")
    product.offer_kind = kind
    product.save(update_fields=["offer_kind"])
    attach_reference_images(
        product=product, uploads=[upload(f"p{i}.png", (600, 600 + i)) for i in range(photos)]
    )
    product.refresh_from_db()
    return product


@pytest.fixture
def oil(growth: Any, make_png_upload: Any) -> Any:
    return make_product(growth, make_png_upload)


def avatar_for(workspace: Any, upload: Any, **fields: Any) -> Avatar:
    portrait = ingest_media(workspace=workspace, upload=upload("face.png", (512, 512)))
    defaults: dict[str, Any] = {
        "name": "Lina",
        "kind": AvatarKind.SYNTHETIC,
        "languages": ["en", "fr"],
        "traits": {"age": "age-30s"},
    }
    return Avatar.objects.create(workspace=workspace, portrait=portrait, **{**defaults, **fields})


@pytest.fixture
def lina(growth: Any, make_png_upload: Any) -> Avatar:
    return avatar_for(growth, make_png_upload)


def template_clip(product: Any, **overrides: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": "clip-10",
        "template": "market-stroll",
        "product": product.pk,
        "language": "en",
        **overrides,
    }


def confirmed(client: Any, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(ESTIMATE, body, format="json")
    assert response.status_code == 200, response.json()
    return {**body, "estimate_id": response.json()["estimate_id"]}


def code_of(response: Any) -> str:
    return str(response.json()["error"]["code"])


# -----------------------------------------------------------------------------
# The catalog
# -----------------------------------------------------------------------------
def test_every_seeded_template_is_valid_and_admin_edits_are_validated(catalog: None) -> None:
    for spec in TEMPLATES:
        ad_templates.validate_metadata(spec["metadata"])
    row = CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key="pour-shot")
    row.metadata = {**row.metadata, "tier": "premium", "units": {}}
    with pytest.raises(DjangoValidationError) as raised:
        row.full_clean()
    problems = " ".join(raised.value.message_dict["metadata"])
    assert "tier:" in problems and "units:" in problems


def test_the_seed_never_overwrites_an_operators_edit(catalog: None) -> None:
    CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key="pour-shot").update(
        label="Pour (edited)"
    )
    call_command("seed_creative_options", verbosity=0)
    assert CreativeOption.objects.get(kind="ad_template", key="pour-shot").label == "Pour (edited)"


# -----------------------------------------------------------------------------
# The gallery
# -----------------------------------------------------------------------------
def test_templates_are_on_from_deploy_with_no_flag_row(
    auth_client: Any, workspace: Any, catalog: None, plans: Any
) -> None:
    on_plan(workspace, plans, "spark")
    assert not FeatureFlag.objects.filter(key=AD_TEMPLATES_S17).exists()
    assert auth_client.get(GALLERY).status_code == 200


def test_flag_off_is_the_studio_as_it_was(
    auth_client: Any, workspace: Any, catalog: None, make_png_upload: Any, plans: Any
) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=False)
    on_plan(workspace, plans, "growth")
    product = make_product(workspace, make_png_upload)
    assert auth_client.get(GALLERY).status_code == 404
    assert (
        auth_client.post(KEYFRAME, {"template": "pour-shot", "product": product.pk}).status_code
        == 404
    )
    assert ad_templates.references_cap(workspace) is None


def test_the_gallery_lists_every_template_with_its_fit_and_lock(
    auth_client: Any, workspace: Any, plans: Any, catalog: None, flag_on: None, make_png_upload: Any
) -> None:
    on_plan(workspace, plans, "essential")
    app = make_product(workspace, make_png_upload, kind="app")
    body = auth_client.get(GALLERY, {"product": app.pk}).json()
    rows = {row["key"]: row for row in body["templates"]}

    assert len(rows) == len(TEMPLATES)
    assert rows["pour-shot"]["fit"] == "template_fit"  # a bottle shot is not an app's
    assert rows["tap-through"]["fit"] == "needs_screenshot"
    assert rows["tap-through"]["lock"] is None
    assert rows["founder-story"]["lock"] == {
        "feature": "ad_templates_talking",
        "suggested_plan": "growth",
        "pack": None,
    }
    assert rows["founder-story"]["available"] is False
    assert body["rerolls"] == 3
    assert body["references_cap"] == 2
    assert [c["key"] for c in body["categories"]][:2] == ["unbox", "hand"]
    assert rows["market-stroll"]["lengths"] == [
        {"key": "clip-10", "label": "10 s", "seconds": 10, "units": 1},
        {"key": "clip-15", "label": "15 s", "seconds": 15, "units": 2},
    ]


def test_spark_sees_people_templates_locked_and_effects_open(
    auth_client: Any, workspace: Any, plans: Any, catalog: None, flag_on: None
) -> None:
    on_plan(workspace, plans, "spark")
    rows = {row["key"]: row for row in auth_client.get(GALLERY).json()["templates"]}
    assert rows["liquid-splash"]["lock"] is None
    assert rows["market-stroll"]["lock"]["feature"] == "ad_templates_people"
    assert rows["pour-shot"]["person_lock"]["feature"] == "ad_templates_people"


def test_an_app_screenshot_tag_clears_the_need(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    app = make_product(growth, make_png_upload, kind="app")
    photo = app.reference_images.first()
    app.reference_tags = {str(photo.pk): "screen"}
    app.save(update_fields=["reference_tags"])
    rows = {r["key"]: r for r in auth_client.get(GALLERY, {"product": app.pk}).json()["templates"]}
    assert rows["tap-through"]["fit"] is None


def test_another_organizations_and_another_brands_product_are_404(
    auth_client: Any, growth: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    stranger = provision_workspace(other_user, name="Stranger Co")
    theirs = make_product(stranger, make_png_upload)
    sibling = provision_extra_workspace(user=user, name="Second brand")
    sibling_product = make_product(sibling, make_png_upload)
    here = {"HTTP_X_WORKSPACE_ID": str(growth.pk)}

    for product in (theirs, sibling_product):
        assert auth_client.get(GALLERY, {"product": product.pk}, **here).status_code == 404
        response = auth_client.post(
            KEYFRAME, {"template": "pour-shot", "product": product.pk}, format="json", **here
        )
        assert response.status_code == 404
        estimate = auth_client.post(ESTIMATE, template_clip(product), format="json", **here)
        assert estimate.status_code == 404


# -----------------------------------------------------------------------------
# Keyframes and re-rolls
# -----------------------------------------------------------------------------
def test_a_keyframe_is_an_image_generation_with_the_person_attached(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    response = auth_client.post(
        KEYFRAME,
        {"template": "market-stroll", "product": oil.pk, "avatar": lina.pk, "location": "market"},
        format="json",
    )
    assert response.status_code == 201, response.json()
    generation = Generation.objects.get(pk=response.json()["id"])
    assert generation.aspect == "9:16"
    assert generation.creative["assets"] == [
        {"media": lina.portrait_id, "role": "person", "note": ""}
    ]
    assert generation.creative["ad_template"]["template"] == "market-stroll"
    assert "market aisle" in generation.prompt and "Olive oil" in generation.prompt
    assert generation.status == "SUCCEEDED"  # eager Celery


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"template": "market-stroll"}, 400, "person_required"),
        ({"template": "nope"}, 400, "validation_error"),
        ({"template": "pour-shot", "location": "moon"}, 400, "template_location"),
        ({"template": "founder-story", "avatar": "LINA"}, 409, "template_unavailable"),
        ({"template": "liquid-splash", "avatar": "LINA"}, 400, "no_person"),
    ],
)
def test_every_keyframe_refusal_has_its_code(
    auth_client: Any, oil: Any, lina: Avatar, body: dict[str, Any], status: int, code: str
) -> None:
    payload = {**body, "product": oil.pk}
    if payload.get("avatar") == "LINA":
        payload["avatar"] = lina.pk
    response = auth_client.post(KEYFRAME, payload, format="json")
    assert response.status_code == status, response.json()
    assert response.json()["error"]["code"] == code


def test_a_template_that_does_not_fit_the_product_is_400(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    app = make_product(growth, make_png_upload, kind="app")
    response = auth_client.post(
        KEYFRAME, {"template": "pour-shot", "product": app.pk}, format="json"
    )
    assert response.status_code == 400
    assert code_of(response) == "template_fit"


def test_a_template_above_the_plan_is_402_with_an_upgrade(
    auth_client: Any, workspace: Any, plans: Any, catalog: None, flag_on: None, make_png_upload: Any
) -> None:
    on_plan(workspace, plans, "spark")
    ledger.grant_credits(workspace, 50, note="test")
    product = make_product(workspace, make_png_upload)
    lina = avatar_for(workspace, make_png_upload)
    response = auth_client.post(
        KEYFRAME, {"template": "pour-shot", "product": product.pk, "avatar": lina.pk}, format="json"
    )
    assert response.status_code == 402
    error = response.json()["error"]
    # Spark has no avatars at all: the person is the first lock it meets.
    assert error["code"] in ("avatar_over_limit", "feature_not_available")
    assert error["upgrade"]["suggested_plan"] == "discovery"


def test_re_rolls_stop_at_the_plans_number(auth_client: Any, oil: Any) -> None:
    first = auth_client.post(KEYFRAME, {"template": "pour-shot", "product": oil.pk}, format="json")
    root = first.json()["id"]
    limit = 5  # growth
    for _ in range(limit):
        again = auth_client.post(
            KEYFRAME, {"template": "pour-shot", "product": oil.pk, "reroll_of": root}, format="json"
        )
        assert again.status_code == 201
    refused = auth_client.post(
        KEYFRAME, {"template": "pour-shot", "product": oil.pk, "reroll_of": root}, format="json"
    )
    assert refused.status_code == 402
    assert code_of(refused) == "reroll_limit_reached"
    assert refused.json()["error"]["detail"] == {"feature": "ad_rerolls", "limit": 5, "used": 5}


def test_a_re_roll_of_another_brands_generation_is_404(
    auth_client: Any, oil: Any, user: Any, make_png_upload: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Sibling")
    theirs = Generation.objects.create(workspace=sibling, user=user, kind="IMAGE", mode="PRODUCT")
    response = auth_client.post(
        KEYFRAME,
        {"template": "pour-shot", "product": oil.pk, "reroll_of": theirs.pk},
        format="json",
        HTTP_X_WORKSPACE_ID=str(oil.workspace_id),
    )
    assert response.status_code == 404, response.json()


def test_a_viewer_cannot_draw_a_keyframe(
    client_as: Any, viewer_user: Any, advanced_workspace: Any, catalog: None, flag_on: None
) -> None:
    viewer = client_as(viewer_user)
    assert viewer.get(GALLERY).status_code == 200
    assert viewer.post(KEYFRAME, {"template": "pour-shot", "product": 1}).status_code == 403


# -----------------------------------------------------------------------------
# Reference caps
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(("plan", "sent"), [("spark", 1), ("essential", 2), ("scale", 5)])
def test_a_render_sends_at_most_the_plans_product_photos(
    workspace: Any,
    plans: Any,
    catalog: None,
    flag_on: None,
    make_png_upload: Any,
    plan: str,
    sent: int,
) -> None:
    on_plan(workspace, plans, plan)
    product = make_product(workspace, make_png_upload, photos=5)
    cap = ad_templates.references_cap(workspace)
    assert len(_reference_image_bytes(product, cap=cap)) == sent


def test_with_the_flag_off_every_photo_is_sent(
    workspace: Any, plans: Any, catalog: None, make_png_upload: Any
) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=False)
    on_plan(workspace, plans, "essential")
    product = make_product(workspace, make_png_upload, photos=4)
    assert ad_templates.references_cap(workspace) is None
    assert len(_reference_image_bytes(product, cap=None)) == 4


# -----------------------------------------------------------------------------
# Video units
# -----------------------------------------------------------------------------
def test_a_template_render_is_priced_in_units_and_makes_its_own_first_frame(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    body = template_clip(oil, avatar=lina.pk, length="clip-15")
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    assert estimate["currency"] == "units"
    assert estimate["units"] == 2 and estimate["credits"] == 0
    assert estimate["keyframe_credits"] > 0

    credits_before = ledger.credit_balance(oil.workspace)
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    assert response.status_code == 202, response.json()
    render = VideoRender.objects.get(pk=response.json()["id"])

    assert render.status == VideoRenderStatus.SUCCEEDED
    assert render.keyframe is not None and render.source_id is not None
    assert render.unit_charge is not None and render.unit_charge.delta == -2
    assert render.charge is None
    assert ledger.video_balance(oil.workspace) == 7
    assert ledger.credit_balance(oil.workspace) < credits_before  # the frame, in credits
    assert render.labels == ["ai_generated"]
    assert response.json()["charged_units"] == 2


def test_a_picked_first_frame_is_animated_without_a_second_one(
    auth_client: Any, oil: Any, make_png_upload: Any
) -> None:
    still = ingest_media(workspace=oil.workspace, upload=make_png_upload("frame.png", (540, 960)))
    body = template_clip(oil, template="pour-shot", source=still.pk)
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    assert estimate["keyframe_credits"] == 0
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.keyframe is None and render.source_id == still.pk
    assert render.labels == []


def test_plain_clips_are_paid_in_units_too_with_the_flag_on(
    auth_client: Any, oil: Any, make_png_upload: Any
) -> None:
    still = ingest_media(workspace=oil.workspace, upload=make_png_upload())
    body = {
        "mode": "clip",
        "length": "clip-10",
        "source": still.pk,
        "motion": "push",
        "aspect": "9-16",
    }
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    assert (estimate["currency"], estimate["units"]) == ("units", 1)
    auth_client.post(RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json")
    assert VideoLedger.objects.filter(workspace=oil.workspace, delta=-1).count() == 1
    assert not CreditLedger.objects.filter(note__startswith="video render").exists()


def test_held_units_count_against_what_is_left(
    auth_client: Any, oil: Any, make_png_upload: Any, monkeypatch: Any
) -> None:
    still = ingest_media(workspace=oil.workspace, upload=make_png_upload())
    monkeypatch.setattr("ai.tasks.run_video_render.delay", lambda pk: None)  # stays QUEUED
    body = template_clip(oil, template="pour-shot", source=still.pk, length="clip-15")
    for _ in range(4):  # 4 x 2 units held of 9
        assert (
            auth_client.post(RENDERS, confirmed(auth_client, body), format="json").status_code
            == 202
        )
    assert video.held_units(oil.workspace) == 8
    refused = auth_client.post(RENDERS, confirmed(auth_client, body), format="json")
    assert refused.status_code == 402
    assert code_of(refused) == "insufficient_video_units"
    assert refused.json()["error"]["detail"] == {"required": 2, "available": 1, "held": 8}


def test_a_failed_render_takes_no_units(
    auth_client: Any, oil: Any, make_png_upload: Any, monkeypatch: Any
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("down")

    monkeypatch.setattr(_fake_video_provider, "animate", broken)
    still = ingest_media(workspace=oil.workspace, upload=make_png_upload())
    body = template_clip(oil, template="pour-shot", source=still.pk)
    response = auth_client.post(RENDERS, confirmed(auth_client, body), format="json")
    assert response.json()["status"] == "FAILED"
    assert response.json()["charged_units"] == 0
    assert ledger.video_balance(oil.workspace) == 9


def test_a_failed_first_frame_fails_the_render_and_takes_nothing(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    from ai.providers import fake

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("down")

    monkeypatch.setattr(fake._fake_image_provider, "generate", broken)
    credits = ledger.credit_balance(oil.workspace)
    response = auth_client.post(
        RENDERS, confirmed(auth_client, template_clip(oil, template="pour-shot")), format="json"
    )
    assert response.status_code == 202, response.json()
    assert response.json()["status"] == "FAILED"
    assert response.json()["error_code"] == "keyframe_failed"
    assert ledger.video_balance(oil.workspace) == 9
    assert ledger.credit_balance(oil.workspace) == credits


def test_spark_renders_an_effects_template_on_its_free_clips(
    auth_client: Any, workspace: Any, plans: Any, catalog: None, flag_on: None, make_png_upload: Any
) -> None:
    on_plan(workspace, plans, "spark")
    ledger.grant_credits(workspace, 50, note="test")
    product = make_product(workspace, make_png_upload)
    body = template_clip(product, template="liquid-splash", length="clip-5")
    estimate = auth_client.post(ESTIMATE, body, format="json").json()
    assert estimate["units"] == 0 and estimate["credits"] == 0
    response = auth_client.post(
        RENDERS, {**body, "estimate_id": estimate["estimate_id"]}, format="json"
    )
    assert response.json()["status"] == "SUCCEEDED"
    assert VideoRender.objects.get(pk=response.json()["id"]).free is True


def test_the_render_queue_reports_units_with_the_flag_on(auth_client: Any, oil: Any) -> None:
    body = auth_client.get(RENDERS).json()
    assert body["units"] == {"balance": 9, "held": 0}


# -----------------------------------------------------------------------------
# The AI label
# -----------------------------------------------------------------------------
def test_a_person_on_screen_is_labelled_through_to_the_draft_and_the_checks(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    from checks.services import run_checks

    body = template_clip(oil, avatar=lina.pk)
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()
    auth_client.patch(f"{RENDERS}{render['id']}/", {"review": "ACCEPTED"}, format="json")
    sent = auth_client.post(f"{RENDERS}send/", {"renders": [render["id"]]}, format="json").json()

    from content.models import Post

    post = Post.objects.get(pk=sent["posts"][0])
    assert post.master_body.endswith("AI-generated")
    results = [r for r in run_checks(post).results if r["key"] == "disclosure"]
    assert results[0]["status"] == CheckStatus.PASS

    post.master_body = "Watch this."
    post.save(update_fields=["master_body"])
    results = [r for r in run_checks(post).results if r["key"] == "disclosure"]
    assert results[0]["status"] == CheckStatus.BLOCK


def test_the_label_blocks_scheduling_even_with_the_checks_step_off(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    from checks.services import ChecksBlockedError, ensure_not_blocked
    from content.models import Post

    FeatureFlag.objects.create(
        organization=oil.workspace.organization, key=CHECKS_S4, enabled=False
    )
    body = template_clip(oil, avatar=lina.pk)
    render = auth_client.post(RENDERS, confirmed(auth_client, body), format="json").json()
    auth_client.patch(f"{RENDERS}{render['id']}/", {"review": "ACCEPTED"}, format="json")
    sent = auth_client.post(f"{RENDERS}send/", {"renders": [render["id"]]}, format="json").json()
    post = Post.objects.get(pk=sent["posts"][0])
    assert ensure_not_blocked(post) is None  # labelled: passes, no S4 run made

    post.master_body = "Watch this."
    post.save(update_fields=["master_body"])
    with pytest.raises(ChecksBlockedError):
        ensure_not_blocked(post)


def test_the_label_follows_the_language_and_is_never_doubled() -> None:
    assert disclosure.with_disclosure("Bonjour", ["ai_generated"], language="fr").endswith(
        "Généré par IA"
    )
    once = disclosure.with_disclosure("Hi\n\nAI-generated", ["ai_generated"])
    assert once.count("AI-generated") == 1
    assert disclosure.with_disclosure("Hi", []) == "Hi"
    assert not disclosure.has_disclosure("#aisle finds")


def test_a_still_made_with_an_avatar_is_labelled(
    auth_client: Any, oil: Any, lina: Avatar, catalog: None
) -> None:
    response = auth_client.post(
        "/api/v1/ai/generate/",
        {
            "kind": "IMAGE",
            "mode": "PRODUCT",
            "product": oil.pk,
            "prompt": "On a table",
            "avatar": lina.pk,
            "n": 1,
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    generation = Generation.objects.get(pk=response.json()["id"])
    assert generation.creative["avatar"] == lina.pk
    assert {"media": lina.portrait_id, "role": "person", "note": ""} in generation.creative[
        "assets"
    ]


def test_a_client_cannot_name_any_picture_a_person(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    response = auth_client.post(
        "/api/v1/ai/generate/",
        {
            "kind": "IMAGE",
            "mode": "PRODUCT",
            "product": oil.pk,
            "prompt": "x",
            "brand_assets": [{"media": lina.portrait_id, "role": "person"}],
        },
        format="json",
    )
    assert response.status_code == 400


def test_the_product_form_sets_what_the_product_is_and_the_gallery_follows(
    auth_client: Any, oil: Any
) -> None:
    response = auth_client.patch(
        f"/api/v1/products/{oil.pk}/", {"offer_kind": "app"}, format="json"
    )
    assert response.status_code == 200, response.json()
    assert response.json()["offer_kind"] == "app"
    rows = {r["key"]: r for r in auth_client.get(GALLERY, {"product": oil.pk}).json()["templates"]}
    assert rows["pour-shot"]["fit"] == "template_fit"
    bad = auth_client.patch(f"/api/v1/products/{oil.pk}/", {"offer_kind": "car"}, format="json")
    assert bad.status_code == 400


def test_language_matters_only_when_the_person_speaks(
    auth_client: Any, oil: Any, lina: Avatar
) -> None:
    # Lina speaks en/fr; an Italian caption on a silent template is fine.
    ok = auth_client.post(
        ESTIMATE, template_clip(oil, avatar=lina.pk, language="it"), format="json"
    )
    assert ok.status_code == 200, ok.json()
