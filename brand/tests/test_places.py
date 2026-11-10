"""S17-28 — the brand's own places, and the places it keeps (`studio_content_audit.md` §3.4).

**Your place.** A photo the brand uploads of its own shop, kitchen, salon or
workshop becomes a place any template can be set in (`own:<id>`): the photo
travels to the image model as the place, never as the product.

**A kept place.** A catalogue place made once for this brand — an empty set,
no product, no people — and attached to every first frame set there, so the
same kitchen appears in every ad. Made by the image pipeline, paid on pass.

Every id is scoped to the request's brand: another brand's or another
organization's place is a 404; with `ad_templates_s17` off the routes do not
exist.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from ai.models import Generation, GenerationKind
from billing.models import FeatureFlag
from billing.services import ledger
from billing.services.flags import AD_TEMPLATES_S17
from brand.models import BrandPlace, PlaceSource
from brand.services import places
from content.services.media import ingest_media
from products.services.products import attach_reference_images, create_product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

PLACES = "/api/v1/brand/places/"
KEEP = f"{PLACES}keep/"
KEYFRAME = "/api/v1/ai/ad-templates/keyframe/"
GALLERY = "/api/v1/ai/ad-templates/"


@pytest.fixture
def growth(workspace: Any, plans: dict[str, Any], db: None, generation_costs: None) -> Any:
    call_command("seed_creative_options", verbosity=0)
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=True)
    workspace.organization.plan = plans["growth"]
    workspace.organization.save(update_fields=["plan"])
    ledger.grant_credits(workspace, 200, note="test")
    return workspace


def photo(workspace: Any, upload: Any, name: str = "shop.png") -> int:
    return int(ingest_media(workspace=workspace, upload=upload(name, (900, 1600))).pk)


def product_in(workspace: Any, upload: Any) -> Any:
    product = create_product(workspace=workspace, name="Olive oil 500 ml")
    attach_reference_images(product=product, uploads=[upload("p.png", (600, 600))])
    product.refresh_from_db()
    return product


# -----------------------------------------------------------------------------
# Your place
# -----------------------------------------------------------------------------
def test_an_uploaded_photo_becomes_a_ready_place(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    media = photo(growth, make_png_upload)
    response = auth_client.post(PLACES, {"media": media, "label": "Our shop"}, format="json")

    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["label"], body["origin"], body["status"]) == ("Our shop", "upload", "ready")
    assert body["key"] == f"own:{body['id']}" and body["image"]
    listed = auth_client.get(PLACES).json()
    assert [row["key"] for row in listed["places"]] == [body["key"]]
    assert listed["keep_credits"] > 0


def test_a_place_needs_an_image_of_this_brand(
    auth_client: Any, growth: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Sibling")
    stranger = provision_workspace(other_user, name="Stranger")
    here = {"HTTP_X_WORKSPACE_ID": str(growth.pk)}
    for workspace in (sibling, stranger):
        theirs = photo(workspace, make_png_upload)
        response = auth_client.post(PLACES, {"media": theirs, "label": "x"}, format="json", **here)
        assert response.status_code == 404
    assert auth_client.post(PLACES, {"label": "x"}, format="json").status_code == 400


def test_the_number_of_places_is_bounded(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    for index in range(places.MAX_PLACES):
        BrandPlace.objects.create(
            workspace=growth,
            label=f"p{index}",
            source=PlaceSource.UPLOAD,
            media_id=photo(growth, make_png_upload, f"{index}.png"),
        )
    response = auth_client.post(
        PLACES, {"media": photo(growth, make_png_upload), "label": "one more"}, format="json"
    )
    assert response.status_code == 400


def test_a_template_can_be_set_in_your_place_and_the_photo_goes_as_the_place(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    product = product_in(growth, make_png_upload)
    media = photo(growth, make_png_upload)
    own = auth_client.post(PLACES, {"media": media, "label": "Our shop"}, format="json").json()

    response = auth_client.post(
        KEYFRAME,
        {"template": "pour-shot", "product": product.pk, "location": own["key"]},
        format="json",
    )

    assert response.status_code == 201, response.json()
    generation = Generation.objects.filter(workspace=growth).latest("id")
    assert {"media": media, "role": "place", "note": ""} in generation.creative["assets"]
    assert "the brand's own place" in generation.prompt_sent
    assert "is the place" in generation.prompt_sent  # the attached photo is named
    gallery = auth_client.get(f"{GALLERY}?product={product.pk}").json()
    assert [p["key"] for p in gallery["own_places"]] == [own["key"]]


def test_another_brands_place_is_404_at_the_keyframe_and_on_delete(
    auth_client: Any, growth: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    product = product_in(growth, make_png_upload)
    here = {"HTTP_X_WORKSPACE_ID": str(growth.pk)}
    for workspace in (
        provision_extra_workspace(user=user, name="Sibling"),
        provision_workspace(other_user, name="Stranger"),
    ):
        theirs = BrandPlace.objects.create(
            workspace=workspace,
            label="Theirs",
            source=PlaceSource.UPLOAD,
            media_id=photo(workspace, make_png_upload),
        )
        keyframe = auth_client.post(
            KEYFRAME,
            {"template": "pour-shot", "product": product.pk, "location": f"own:{theirs.pk}"},
            format="json",
            **here,
        )
        assert keyframe.status_code == 404
        assert auth_client.delete(f"{PLACES}{theirs.pk}/", **here).status_code == 404
        assert theirs.pk not in [p["id"] for p in auth_client.get(PLACES, **here).json()["places"]]


def test_a_deleted_place_is_gone_but_its_photo_stays(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    media = photo(growth, make_png_upload)
    own = auth_client.post(PLACES, {"media": media, "label": "Shop"}, format="json").json()

    assert auth_client.delete(f"{PLACES}{own['id']}/").status_code == 204
    assert auth_client.get(PLACES).json()["places"] == []
    from content.models import MediaAsset

    assert MediaAsset.objects.filter(pk=media).exists()  # media is immutable, never deleted here


# -----------------------------------------------------------------------------
# A kept place
# -----------------------------------------------------------------------------
def test_keeping_a_place_makes_its_plate_once_and_charges_on_pass(
    auth_client: Any, growth: Any
) -> None:
    before = ledger.credit_balance(growth)
    response = auth_client.post(KEEP, {"place": "kitchen"}, format="json")

    assert response.status_code == 201, response.json()
    kept = response.json()
    assert (kept["key"], kept["origin"], kept["status"]) == ("kitchen", "made", "ready")
    generation = Generation.objects.get(pk=BrandPlace.objects.get(pk=kept["id"]).generation_id)
    assert generation.kind == GenerationKind.IMAGE and generation.product_id is None
    assert "no people" in generation.prompt and "no product" in generation.prompt
    assert ledger.credit_balance(growth) == before - kept["credits"]

    again = auth_client.post(KEEP, {"place": "kitchen"}, format="json")
    assert again.status_code == 200 and again.json()["id"] == kept["id"]  # never made twice
    assert ledger.credit_balance(growth) == before - kept["credits"]


def test_a_kept_place_goes_with_every_first_frame_set_there(
    auth_client: Any, growth: Any, make_png_upload: Any
) -> None:
    product = product_in(growth, make_png_upload)
    kept = auth_client.post(KEEP, {"place": "kitchen"}, format="json").json()
    plate = BrandPlace.objects.get(pk=kept["id"])

    auth_client.post(
        KEYFRAME,
        {"template": "pour-shot", "product": product.pk, "location": "kitchen"},
        format="json",
    )
    generation = Generation.objects.filter(workspace=growth, product=product).latest("id")
    assert {"media": plate.media_id, "role": "place", "note": ""} in generation.creative["assets"]

    gallery = auth_client.get(f"{GALLERY}?product={product.pk}").json()
    assert "kitchen" in gallery["kept"]


def test_keeping_an_unknown_place_is_400_and_without_credits_402(
    auth_client: Any, growth: Any
) -> None:
    assert auth_client.post(KEEP, {"place": "moon"}, format="json").status_code == 400
    from billing.services.entitlements import entitlements_for

    ledger.debit_credits(
        growth,
        ledger.credit_balance(growth),
        quota=entitlements_for(growth).quota("monthly_ai_credits"),
        note="spend it all",
    )
    response = auth_client.post(KEEP, {"place": "kitchen"}, format="json")
    assert response.status_code == 402


def test_a_failed_plate_can_be_made_again(auth_client: Any, growth: Any, monkeypatch: Any) -> None:
    from ai.providers.fake import _fake_image_provider
    from common.exceptions import ProviderError

    def down(**kwargs: Any) -> Any:
        raise ProviderError("down")

    monkeypatch.setattr(_fake_image_provider, "generate", down)
    before = ledger.credit_balance(growth)
    auth_client.post(KEEP, {"place": "terrace"}, format="json")  # eager: the vendor's error
    failed = BrandPlace.objects.get(workspace=growth, place_key="terrace")
    assert places.status_of(failed) == "failed"
    listed = auth_client.get(PLACES).json()["places"]
    assert [p["status"] for p in listed] == ["failed"]
    assert ledger.credit_balance(growth) == before  # nothing taken for a failed plate
    monkeypatch.undo()

    again = auth_client.post(KEEP, {"place": "terrace"}, format="json").json()
    assert again["id"] != failed.pk and again["status"] == "ready"
    assert not BrandPlace.objects.filter(pk=failed.pk).exists()


# -----------------------------------------------------------------------------
# Permission and flag
# -----------------------------------------------------------------------------
def test_viewing_needs_view_and_changing_needs_edit(
    client_as: Any, growth: Any, make_png_upload: Any
) -> None:
    from django.contrib.auth import get_user_model

    from workspaces.models import Membership, Role

    reader = get_user_model().objects.create_user(email="reader@example.com", password="x")
    Membership.objects.create(user=reader, workspace=growth, role=Role.VIEWER)
    viewer = client_as(reader)
    here = {"HTTP_X_WORKSPACE_ID": str(growth.pk)}

    assert viewer.get(PLACES, **here).status_code == 200
    assert viewer.post(KEEP, {"place": "kitchen"}, format="json", **here).status_code == 403
    media = photo(growth, make_png_upload)
    assert (
        viewer.post(PLACES, {"media": media, "label": "x"}, format="json", **here).status_code
        == 403
    )


def test_flag_off_the_place_routes_do_not_exist(auth_client: Any, workspace: Any) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=False)
    assert auth_client.get(PLACES).status_code == 404
    assert auth_client.post(KEEP, {"place": "kitchen"}, format="json").status_code == 404
