# ruff: noqa: F811 — the S17 suite's fixtures are imported, then named as parameters
"""S17 — premium templates, the template pack, and several people or products in
one ad (`ads_templates.md`, amended 2026-10-27).

A premium template opens on a plan with `ad_templates_premium` or for an
organization that bought the `templates-premium` pack; the add-on the pack
grants cannot be switched on for free. How many avatars and products one ad
may show is the plan's `ad_avatars_per_ad` / `ad_products_per_ad`.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from ai.models import Generation, VideoRender
from ai.services import ad_templates
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    GALLERY,
    KEYFRAME,
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
from billing.models import AddonStatus, OrganizationAddon, Pack
from billing.services import ledger, purchases, webhooks
from billing.tests.test_purchases import _paid_session
from common.exceptions import OCCSError
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PREMIUM = "stair-unbox"  # premium, people tier


@pytest.fixture
def pack(db: None) -> Pack:
    call_command("seed_packs", verbosity=0)
    row = Pack.objects.get(code=ad_templates.PREMIUM_PACK)
    row.stripe_price_id = "price_templates"
    row.save()
    return row


@pytest.fixture
def essential(workspace: Any, plans: Any, catalog: None, flag_on: None) -> Any:
    on_plan(workspace, plans, "essential")
    ledger.grant_credits(workspace, 200, note="test")
    ledger.grant_video_units(workspace, 9, note="test")
    return workspace


# -----------------------------------------------------------------------------
# Premium templates
# -----------------------------------------------------------------------------
def test_a_premium_template_is_locked_below_growth_with_the_pack_to_buy(
    auth_client: Any, essential: Any, pack: Pack
) -> None:
    rows = {r["key"]: r for r in auth_client.get(GALLERY).json()["templates"]}
    assert rows[PREMIUM]["premium"] is True
    assert rows[PREMIUM]["lock"] == {
        "feature": "ad_templates_premium",
        "suggested_plan": "growth",
        "pack": "templates-premium",
    }
    assert rows["pour-shot"]["premium"] is False and rows["pour-shot"]["lock"] is None


def test_without_a_public_pack_the_lock_offers_only_the_plan(
    auth_client: Any, essential: Any
) -> None:
    rows = {r["key"]: r for r in auth_client.get(GALLERY).json()["templates"]}
    assert rows[PREMIUM]["lock"]["pack"] is None


def test_a_premium_template_is_402_at_the_server(
    auth_client: Any, essential: Any, make_png_upload: Any
) -> None:
    product = make_product(essential, make_png_upload)
    lina = avatar_for(essential, make_png_upload)
    response = auth_client.post(
        KEYFRAME, {"template": PREMIUM, "product": product.pk, "avatar": lina.pk}, format="json"
    )
    assert response.status_code == 402, response.json()
    error = response.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["detail"]["feature"] == "ad_templates_premium"
    assert error["upgrade"]["suggested_plan"] == "growth"


def test_growth_has_premium_templates_in_the_plan(auth_client: Any, oil: Any) -> None:
    rows = {r["key"]: r for r in auth_client.get(GALLERY).json()["templates"]}
    assert rows[PREMIUM]["lock"] is None


def test_the_tier_lock_comes_before_the_premium_lock(
    auth_client: Any, workspace: Any, plans: Any, catalog: None, flag_on: None
) -> None:
    on_plan(workspace, plans, "spark")
    rows = {r["key"]: r for r in auth_client.get(GALLERY).json()["templates"]}
    assert rows[PREMIUM]["lock"]["feature"] == "ad_templates_people"


def test_buying_the_pack_unlocks_premium_for_every_brand_of_the_organization(
    auth_client: Any, essential: Any, pack: Pack, user: Any, make_png_upload: Any
) -> None:
    purchases.start_purchase(
        essential, pack_code="templates-premium", success_url="s", cancel_url="c"
    )
    assert webhooks.process_event(_paid_session(essential, "templates-premium")) is True

    addon = OrganizationAddon.objects.get(organization=essential.organization)
    assert addon.addon_key == "ad_templates_premium"
    assert addon.status == AddonStatus.ACTIVE and addon.trial_ends_at is None

    sibling = provision_extra_workspace(user=user, name="Second brand")
    for brand in (essential, sibling):
        rows = {
            r["key"]: r
            for r in auth_client.get(GALLERY, HTTP_X_WORKSPACE_ID=str(brand.pk)).json()["templates"]
        }
        assert rows[PREMIUM]["lock"] is None

    product = make_product(essential, make_png_upload)
    lina = avatar_for(essential, make_png_upload)
    response = auth_client.post(
        KEYFRAME,
        {"template": PREMIUM, "product": product.pk, "avatar": lina.pk},
        format="json",
        HTTP_X_WORKSPACE_ID=str(essential.pk),
    )
    assert response.status_code == 201, response.json()


def test_the_pack_is_refused_when_premium_is_already_open(oil: Any, pack: Pack) -> None:
    with pytest.raises(OCCSError) as raised:
        purchases.start_purchase(
            oil.workspace, pack_code="templates-premium", success_url="s", cancel_url="c"
        )
    assert raised.value.code == "already_unlocked"


def test_the_pack_is_refused_a_second_time(essential: Any, pack: Pack) -> None:
    webhooks.process_event(_paid_session(essential, "templates-premium"))
    with pytest.raises(OCCSError) as raised:
        purchases.start_purchase(
            essential, pack_code="templates-premium", success_url="s", cancel_url="c"
        )
    assert raised.value.code == "already_unlocked"


def test_the_premium_add_on_cannot_be_switched_on_for_free(
    auth_client: Any, essential: Any
) -> None:
    response = auth_client.post(
        "/api/v1/billing/addons/",
        {"addon_key": "ad_templates_premium", "enabled": True},
        format="json",
    )
    assert response.status_code == 400
    assert not OrganizationAddon.objects.filter(addon_key="ad_templates_premium").exists()


def test_premium_must_be_true_or_false(catalog: None) -> None:
    from django.core.exceptions import ValidationError as DjangoValidationError

    from ai.ad_template_seed import TEMPLATES

    meta = {**TEMPLATES[0]["metadata"], "premium": "yes"}
    with pytest.raises(DjangoValidationError):
        ad_templates.validate_metadata(meta)


# -----------------------------------------------------------------------------
# Several avatars and products in one ad
# -----------------------------------------------------------------------------
def test_the_gallery_reports_the_cast_limits(auth_client: Any, oil: Any) -> None:
    body = auth_client.get(GALLERY).json()
    assert body["avatars_per_ad"] == 2 and body["products_per_ad"] == 3


def test_a_keyframe_carries_every_person_and_product(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any
) -> None:
    sami = avatar_for(oil.workspace, make_png_upload, name="Sami")
    soap = make_product(oil.workspace, make_png_upload)
    soap.name = "Olive soap"
    soap.save(update_fields=["name"])
    response = auth_client.post(
        KEYFRAME,
        {
            "template": "market-stroll",
            "product": oil.pk,
            "avatar": lina.pk,
            "extra_avatars": [sami.pk],
            "extra_products": [soap.pk],
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    generation = Generation.objects.get(pk=response.json()["id"])
    assets = generation.creative["assets"]
    assert [a["role"] for a in assets] == ["person", "person", "product"]
    assert assets[1]["media"] == sami.portrait_id
    assert assets[2]["media"] == soap.reference_images.order_by("pk").first().pk
    assert "Olive oil 500 ml and Olive soap" in generation.prompt
    assert generation.creative["ad_template"]["extra_avatars"] == [sami.pk]
    assert generation.creative["ad_template"]["extra_products"] == [soap.pk]


@pytest.mark.parametrize(
    ("field", "feature"), [("avatars", "ad_avatars_per_ad"), ("products", "ad_products_per_ad")]
)
def test_more_than_the_plan_allows_is_402(
    auth_client: Any, essential: Any, make_png_upload: Any, field: str, feature: str
) -> None:
    product = make_product(essential, make_png_upload)
    lina = avatar_for(essential, make_png_upload)
    extras = (
        [avatar_for(essential, make_png_upload, name="Sami").pk]
        if field == "avatars"
        else [make_product(essential, make_png_upload).pk for _ in range(2)]
    )
    response = auth_client.post(
        KEYFRAME,
        {
            "template": "market-stroll",
            "product": product.pk,
            "avatar": lina.pk,
            f"extra_{field}": extras,
        },
        format="json",
    )
    assert response.status_code == 402, response.json()
    error = response.json()["error"]
    assert error["code"] == "cast_limit_reached"
    assert error["detail"]["feature"] == feature
    assert error["upgrade"]["suggested_plan"] == "growth"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"extra_avatars": ["SAMI"]}, "validation_error"),  # no main avatar
        ({"avatar": "LINA", "extra_avatars": ["LINA"]}, "validation_error"),
        ({"avatar": "LINA", "extra_products": ["OIL"]}, "validation_error"),
    ],
)
def test_a_cast_that_repeats_or_skips_the_main_one_is_400(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any, body: dict[str, Any], code: str
) -> None:
    sami = avatar_for(oil.workspace, make_png_upload, name="Sami")
    names = {"LINA": lina.pk, "SAMI": sami.pk, "OIL": oil.pk}
    payload: dict[str, Any] = {"template": "pour-shot", "product": oil.pk}
    for key, value in body.items():
        payload[key] = [names[v] for v in value] if isinstance(value, list) else names[value]
    response = auth_client.post(KEYFRAME, payload, format="json")
    assert response.status_code == 400, response.json()
    assert code_of(response) == code


def test_another_brands_or_organizations_extra_is_404(
    auth_client: Any, oil: Any, lina: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    stranger = provision_workspace(other_user, name="Stranger Co")
    sibling = provision_extra_workspace(user=user, name="Second brand")
    here = {"HTTP_X_WORKSPACE_ID": str(oil.workspace_id)}
    for brand in (stranger, sibling):
        theirs_product = make_product(brand, make_png_upload)
        theirs_avatar = avatar_for(brand, make_png_upload)
        for extra in (
            {"extra_products": [theirs_product.pk]},
            {"extra_avatars": [theirs_avatar.pk]},
        ):
            response = auth_client.post(
                KEYFRAME,
                {"template": "market-stroll", "product": oil.pk, "avatar": lina.pk, **extra},
                format="json",
                **here,
            )
            assert response.status_code == 404, response.json()


def test_a_render_keeps_the_cast_and_draws_it_in_the_first_frame(
    auth_client: Any, oil: Any, lina: Any, make_png_upload: Any
) -> None:
    sami = avatar_for(oil.workspace, make_png_upload, name="Sami")
    body = template_clip(oil, avatar=lina.pk, extra_avatars=[sami.pk])
    response = auth_client.post(RENDERS, confirmed(auth_client, body), format="json")
    assert response.status_code == 202, response.json()
    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.spec["extra_avatars"] == [sami.pk]
    assert render.keyframe is not None
    assert [a["role"] for a in render.keyframe.creative["assets"]] == ["person", "person"]


def test_the_estimate_refuses_a_cast_over_the_plan(
    auth_client: Any, essential: Any, make_png_upload: Any
) -> None:
    product = make_product(essential, make_png_upload)
    lina = avatar_for(essential, make_png_upload)
    sami = avatar_for(essential, make_png_upload, name="Sami")
    response = auth_client.post(
        ESTIMATE, template_clip(product, avatar=lina.pk, extra_avatars=[sami.pk]), format="json"
    )
    assert response.status_code == 402
    assert code_of(response) == "cast_limit_reached"
