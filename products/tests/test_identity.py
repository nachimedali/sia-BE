"""S17-29 — the product identity card and clean photos (`studio_content_audit.md` §4.3).

**The card.** The model drifts on labels; words and pixels together hold them
better than either alone (goose-skills `prompts.md` §1). A vision call drafts
what the product looks like — shape, colours, the label's exact words, what
must never change — and the person corrects and confirms it. Only a confirmed
card is sent, after the product's photos, on every image of the product.

**A clean photo.** A photo with props or other products next to the product
gets fused onto it by the model (goose Seedance rule 0c). Cleaning makes a new
picture of the product alone, from that photo only, and adds it as a `front`
photo; the original stays (media is immutable).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from ai.models import Generation, GenerationMode, GenerationStatus
from ai.services import prompting
from billing.services import ledger
from content.services.media import ingest_media
from products.models import Product
from products.services import identity
from products.services.products import attach_reference_images, create_product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

CARD: dict[str, Any] = {
    "shape": "tall square glass bottle, rounded shoulders, black screw cap",
    "colours": "dark green glass, cream label, gold band",
    "label": ["OLIVE GOLD", "Extra Virgin", "500 ml"],
    "keep": "the gold band at mid-height; the label centred",
}


@pytest.fixture
def oil(workspace: Any, make_png_upload: Any, generation_costs: None) -> Product:
    call_command("seed_creative_options", verbosity=0)
    ledger.grant_credits(workspace, 100, note="test")
    product = create_product(workspace=workspace, name="Olive Gold 500 ml")
    attach_reference_images(product=product, uploads=[make_png_upload("p.png", (600, 600))])
    product.refresh_from_db()
    return product


def url(product: Product, tail: str) -> str:
    return f"/api/v1/products/{product.pk}/{tail}"


# -----------------------------------------------------------------------------
# The card
# -----------------------------------------------------------------------------
def test_a_draft_is_read_from_the_front_photo_and_paid_on_pass(
    auth_client: Any, oil: Product
) -> None:
    before = ledger.credit_balance(oil.workspace)
    response = auth_client.post(url(oil, "identity/draft/"))

    assert response.status_code == 200, response.json()
    body = response.json()
    card = body["identity"]
    assert card["shape"] and card["colours"] and card["label"] and card["keep"]
    assert body["identity_confirmed_at"] is None  # a draft is never sent
    assert ledger.credit_balance(oil.workspace) == before - identity.draft_credits(oil)


def test_an_unreadable_reply_costs_nothing_and_says_so(
    auth_client: Any, oil: Product, monkeypatch: Any
) -> None:
    from ai.providers.base import TextGenerationResult, TextVariant
    from ai.providers.fake import FakeTextProvider

    def mumble(self: Any, **kwargs: Any) -> Any:
        return TextGenerationResult(
            variants=[TextVariant(body="a bottle, I think", rationale="")],
            provider="fake",
            model="fake",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )

    monkeypatch.setattr(FakeTextProvider, "caption", mumble)
    before = ledger.credit_balance(oil.workspace)
    response = auth_client.post(url(oil, "identity/draft/"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "identity_unreadable"
    assert ledger.credit_balance(oil.workspace) == before


def test_the_person_corrects_and_confirms_the_card(auth_client: Any, oil: Product) -> None:
    response = auth_client.patch(url(oil, "identity/"), {**CARD, "confirmed": True}, format="json")

    assert response.status_code == 200, response.json()
    oil.refresh_from_db()
    assert oil.identity == CARD and oil.identity_confirmed_at is not None

    unconfirmed = auth_client.patch(
        url(oil, "identity/"), {**CARD, "shape": "a can", "confirmed": False}, format="json"
    )
    assert unconfirmed.status_code == 200
    oil.refresh_from_db()
    assert oil.identity["shape"] == "a can" and oil.identity_confirmed_at is None


@pytest.mark.parametrize(
    "bad",
    [
        {"shape": "x" * 200},
        {"label": ["a"] * 7},
        {"label": ["x" * 61]},
        {"label": "OLIVE GOLD"},
    ],
)
def test_a_card_out_of_bounds_is_400(auth_client: Any, oil: Product, bad: dict[str, Any]) -> None:
    response = auth_client.patch(url(oil, "identity/"), {**CARD, **bad}, format="json")
    assert response.status_code == 400


def test_only_a_confirmed_card_is_sent_with_the_photos(oil: Product) -> None:
    oil.identity = CARD
    oil.save(update_fields=["identity"])
    unsent = prompting.assemble_image_prompt(idea="a bottle", workspace=oil.workspace, product=oil)
    assert "OLIVE GOLD" not in unsent.user

    identity.save(oil, CARD, confirmed=True)
    sent = prompting.assemble_image_prompt(idea="a bottle", workspace=oil.workspace, product=oil)
    assert '"OLIVE GOLD", "Extra Virgin", "500 ml"' in sent.user
    assert CARD["shape"] in sent.user and CARD["keep"] in sent.user
    assert sent.grounding["identity"] is True


def test_another_brands_product_is_404(
    auth_client: Any, workspace: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}
    for theirs_workspace in (
        provision_extra_workspace(user=user, name="Sibling"),
        provision_workspace(other_user, name="Stranger"),
    ):
        theirs = create_product(workspace=theirs_workspace, name="Theirs")
        assert auth_client.post(url(theirs, "identity/draft/"), **here).status_code == 404
        assert (
            auth_client.patch(url(theirs, "identity/"), CARD, format="json", **here).status_code
            == 404
        )


# -----------------------------------------------------------------------------
# A clean photo
# -----------------------------------------------------------------------------
def test_cleaning_a_photo_makes_a_new_front_photo_from_that_photo_alone(
    auth_client: Any, oil: Product, make_png_upload: Any
) -> None:
    attach_reference_images(product=oil, uploads=[make_png_upload("busy.png", (700, 500))])
    busy = oil.reference_images.order_by("-pk").first()
    assert busy is not None
    before = set(oil.reference_images.values_list("pk", flat=True))

    started = auth_client.post(url(oil, f"reference-images/{busy.pk}/clean/"))
    assert started.status_code == 202, started.json()
    generation = Generation.objects.get(pk=started.json()["generation"])
    assert generation.source_media_id == busy.pk and generation.mode == GenerationMode.PRODUCT
    assert "alone" in generation.prompt
    assert generation.status == GenerationStatus.SUCCEEDED  # eager under test

    done = auth_client.get(url(oil, f"clean/{generation.pk}/"))
    assert done.status_code == 200 and done.json()["status"] == "ready"
    oil.refresh_from_db()
    added = set(oil.reference_images.values_list("pk", flat=True)) - before
    assert len(added) == 1
    (new,) = added
    assert oil.reference_tags[str(new)] == "front"
    assert busy.pk in oil.reference_images.values_list("pk", flat=True)  # the original stays

    again = auth_client.get(url(oil, f"clean/{generation.pk}/"))
    assert again.json()["status"] == "ready"
    oil.refresh_from_db()
    assert oil.reference_images.count() == len(before) + 1  # read twice, added once


def test_cleaning_sends_only_that_photo_to_the_model(
    auth_client: Any, oil: Product, make_png_upload: Any
) -> None:
    from ai.providers.fake import _fake_image_provider

    attach_reference_images(product=oil, uploads=[make_png_upload("busy.png", (700, 500))])
    busy = oil.reference_images.order_by("-pk").first()
    assert busy is not None
    _fake_image_provider.calls.clear()
    auth_client.post(url(oil, f"reference-images/{busy.pk}/clean/"))

    assert oil.reference_images.count() == 2
    assert _fake_image_provider.calls[-1]["reference_count"] == 1


def test_a_photo_that_is_not_this_products_is_404(
    auth_client: Any, oil: Product, workspace: Any, make_png_upload: Any
) -> None:
    loose = ingest_media(workspace=workspace, upload=make_png_upload("loose.png", (300, 300)))
    assert auth_client.post(url(oil, f"reference-images/{loose.pk}/clean/")).status_code == 404
    assert auth_client.get(url(oil, "clean/999999/")).status_code == 404
