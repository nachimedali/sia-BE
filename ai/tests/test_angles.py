"""S17-34 — angles and hook styles (`studio_content_audit.md` §4.8).

* **Rows, not code.** Ten angles (what the ad argues, by what the viewer
  already knows) and fourteen hook styles (how its first second works), each a
  catalogue row with the instruction the script writer reads.
* **The product's own words.** Who buys it and when (`buyer_situation`), and
  what makes them hesitate (`top_objection`), join the brief with its facts and
  the claims proven on file — and only those.
* **Three styles, not three synonyms.** The writer returns the main hook and
  two more in other styles; "Make 3" varies those.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps

from ai.ad_template_seed import ANGLES, HOOK_FAMILIES
from ai.models import CreativeKind, CreativeOption
from ai.providers.fake import _fake_text_provider
from ai.tests.test_ad_scripts import answer, ask
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    catalog,
    code_of,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ai.migrations.0043_angles_and_hook_families")


@pytest.fixture
def oil(growth: Any, make_png_upload: Any) -> Any:  # noqa: F811
    product = make_product(growth, make_png_upload)
    product.buyer_situation = "Cooks who want good oil on a weekday"
    product.top_objection = "Too expensive for every day"
    product.features = ["cold pressed", "500 ml"]
    product.save(update_fields=["buyer_situation", "top_objection", "features"])
    return product


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_every_angle_names_its_stage_and_hook_styles_that_exist() -> None:
    families = {row["key"] for row in HOOK_FAMILIES}
    assert len(ANGLES) == 10 and len(HOOK_FAMILIES) == 14
    for angle in ANGLES:
        meta = angle["metadata"]
        assert meta["stage"] in ("unaware", "problem", "solution", "product", "any")
        assert meta["families"] and set(meta["families"]) <= families, angle["key"]
        assert angle["prompt_fragment"], angle["key"]
    for family in HOOK_FAMILIES:
        assert family["prompt_fragment"], family["key"]


def test_the_gallery_serves_angles_and_hook_styles_in_the_interface_language(
    auth_client: Any,
    oil: Any,
) -> None:
    body = auth_client.get(f"{GALLERY}?product={oil.pk}", HTTP_ACCEPT_LANGUAGE="fr").json()
    proof = CreativeOption.objects.get(kind=CreativeKind.ANGLE, key="proof")
    angle = next(row for row in body["angles"] if row["key"] == "proof")
    assert angle["label"] == proof.translations["fr"]["label"]
    assert angle["families"] == proof.metadata["families"]
    assert len(body["hook_families"]) == 14


# -----------------------------------------------------------------------------
# The brief
# -----------------------------------------------------------------------------
def test_the_brief_carries_the_angle_the_style_and_the_products_own_words(
    auth_client: Any, oil: Any
) -> None:
    response = ask(auth_client, oil, angle="objection", family="question")

    assert response.status_code == 200, response.json()
    prompt = _fake_text_provider.calls[-1]["prompt"]
    objection = CreativeOption.objects.get(kind=CreativeKind.ANGLE, key="objection")
    question = CreativeOption.objects.get(kind=CreativeKind.HOOK_FAMILY, key="question")
    assert objection.prompt_fragment in prompt and question.prompt_fragment in prompt
    assert "Too expensive for every day" in prompt
    assert "Cooks who want good oil on a weekday" in prompt
    assert "cold pressed" in prompt


def test_an_unknown_angle_or_style_is_400(auth_client: Any, oil: Any) -> None:
    assert ask(auth_client, oil, angle="magic").status_code == 400
    assert ask(auth_client, oil, family="shouting").status_code == 400


def test_three_hooks_in_three_styles_come_back(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    answer(
        monkeypatch,
        "HOOK: Too pricey for a Tuesday?\nBODY: Olive oil 500 ml, cold pressed.\nCTA: Try it.\n"
        "HOOK2: Cold pressed, 500 ml.\nHOOK3: Introducing our oil.",
    )
    body = ask(auth_client, oil, angle="objection", family="question").json()

    assert body["hooks"][0] == "Too pricey for a Tuesday?"
    assert body["hooks"][1] == "Cold pressed, 500 ml."
    assert len(body["hooks"]) == 2  # a dead opener is dropped, never shown
    assert body["families"][0] == "question" and len(body["families"]) == 2
    assert len(set(body["families"])) == 2


def test_without_alternatives_the_one_hook_still_comes_back(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    answer(monkeypatch, "HOOK: Weekday oil.\nBODY: Olive oil 500 ml, cold pressed.\nCTA: Try it.")
    body = ask(auth_client, oil).json()
    assert body["hooks"] == ["Weekday oil."]


# -----------------------------------------------------------------------------
# The product's words
# -----------------------------------------------------------------------------
def test_the_product_keeps_its_buyer_situation_and_objection(auth_client: Any, oil: Any) -> None:
    response = auth_client.patch(
        f"/api/v1/products/{oil.pk}/",
        {"buyer_situation": "Busy parents at 6 pm", "top_objection": "Will it last?"},
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert response.json()["buyer_situation"] == "Busy parents at 6 pm"
    too_long = auth_client.patch(
        f"/api/v1/products/{oil.pk}/", {"top_objection": "x" * 201}, format="json"
    )
    assert too_long.status_code == 400


# -----------------------------------------------------------------------------
# The migration
# -----------------------------------------------------------------------------
def test_the_migration_seeds_both_catalogues_once(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    MIGRATION.forwards(apps, None)
    MIGRATION.forwards(apps, None)
    assert CreativeOption.objects.filter(kind="angle").count() == 10
    assert CreativeOption.objects.filter(kind="hook_family").count() == 14
    assert CreativeOption.objects.get(kind="angle", key="proof").translations["it"]["label"]
