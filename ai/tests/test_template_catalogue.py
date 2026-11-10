"""S17-32 — template catalogue v3 (`studio_content_audit.md` §5.1, §5.3, §5.5).

* **Fewer, clearer templates.** Doubles and ideas that fail the build or swap
  test are retired (inactive, never deleted: old renders keep their key).
* **Rewritten where the idea was weak**: the pour has a cause, before/after
  becomes old way vs new way (no body result), the reaction loses the stock
  shock face.
* **Talking formats** that cannot be made yet are hidden, but three stay as
  "soon": a wall of locked tiles is frustration, not desire.
* **Each template knows its goal** (sell, explain, show, trust) and the one
  detail that kills the idea if lost — sent with its first frame.
* **The gallery recommends three** for the product, of different kinds.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError as DjangoValidationError

from ai.ad_template_seed import GOALS, TEMPLATES
from ai.models import CreativeKind, CreativeOption
from ai.services import ad_templates
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    catalog,
    flag_on,
    growth,
    make_product,
)

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ai.migrations.0041_template_catalogue_v3")
RETIRED = {"lid-asmr", "freeze-frame", "colour-burst", "kitchen-counter"}
SOON = {"founder-story", "three-reasons", "comment-reply"}


def _spec(key: str) -> dict[str, Any]:
    return next(spec for spec in TEMPLATES if spec["key"] == key)


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_retired_templates_are_no_longer_seeded() -> None:
    assert not RETIRED & {spec["key"] for spec in TEMPLATES}


def test_every_template_has_a_goal_and_a_protected_detail() -> None:
    for spec in TEMPLATES:
        meta = spec["metadata"]
        assert meta["goal"] in GOALS, spec["key"]
        assert isinstance(meta["protect"], str) and len(meta["protect"]) > 20, spec["key"]


def test_talking_formats_are_hidden_but_three_stay_as_soon() -> None:
    talking = {spec["key"] for spec in TEMPLATES if spec["metadata"]["tier"] == "talking"}
    shown = {key for key in talking if not _spec(key)["metadata"].get("hidden")}
    assert shown == SOON


def test_the_weak_ideas_are_rewritten() -> None:
    pour = _spec("pour-shot")["metadata"]
    assert "slow-motion pour" not in pour["motion_prompt"].lower()
    assert "tilt" in pour["motion_prompt"]  # the pour has its cause in the frame
    before = _spec("before-after")
    assert before["label"] == "Old way, new way"
    assert "result" not in before["metadata"]["keyframe_prompt"].lower()
    reaction = _spec("reaction-reveal")["metadata"]
    assert "mouth" not in reaction["keyframe_prompt"]


def test_goal_and_hidden_are_held_to_their_type(catalog: None) -> None:  # noqa: F811
    meta = dict(CreativeOption.objects.get(kind="ad_template", key="pour-shot").metadata)
    for bad in ({"goal": "win"}, {"hidden": "yes"}, {"protect": 3}):
        with pytest.raises(DjangoValidationError):
            ad_templates.validate_metadata({**meta, **bad})


# -----------------------------------------------------------------------------
# What reaches the model, and the gallery
# -----------------------------------------------------------------------------
def test_the_protected_detail_goes_with_the_first_frame(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    row = CreativeOption.objects.get(kind="ad_template", key="pour-shot")
    prompt = ad_templates.keyframe_prompt(row, product=product)
    assert f"Protect: {row.metadata['protect']}" in prompt


def test_the_gallery_hides_hidden_templates_and_says_each_ones_goal(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    body = auth_client.get(f"{GALLERY}?product={product.pk}").json()

    keys = {row["key"] for row in body["templates"]}
    assert not {"two-chairs", "investigation"} & keys
    assert keys >= SOON
    assert all(row["goal"] in GOALS for row in body["templates"])
    assert [goal["key"] for goal in body["goals"]] == list(GOALS)


def test_the_gallery_recommends_three_of_different_kinds_that_can_be_made(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    product.campaign_goal = "awareness"
    product.save(update_fields=["campaign_goal"])
    body = auth_client.get(f"{GALLERY}?product={product.pk}").json()

    recommended = body["recommended"]
    assert len(recommended) == 3
    rows = {row["key"]: row for row in body["templates"]}
    for key in recommended:
        row = rows[key]
        assert row["available"] and row["fit"] is None and row["lock"] is None, key
    assert len({rows[key]["category"] for key in recommended}) == 3


def test_recommendations_follow_the_products_goal(
    auth_client: Any,
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    product = make_product(growth, make_png_upload)
    product.campaign_goal = "sales"
    product.save(update_fields=["campaign_goal"])
    body = auth_client.get(f"{GALLERY}?product={product.pk}").json()

    rows = {row["key"]: row for row in body["templates"]}
    assert rows[body["recommended"][0]]["goal"] == "sell"


# -----------------------------------------------------------------------------
# The migration
# -----------------------------------------------------------------------------
@pytest.fixture
def seeding(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def test_the_migration_retires_seeded_rows_and_adds_goals_without_overwriting(
    seeding: None,
    catalog: None,  # noqa: F811
) -> None:
    CreativeOption.objects.create(
        kind="ad_template",
        key="lid-asmr",
        label="Lid lift ASMR",
        metadata={**_spec("slow-unbox")["metadata"], "seed": "v2"},
    )
    own = CreativeOption.objects.create(
        kind="ad_template", key="freeze-frame", label="Ours", metadata={"category": "fx"}
    )
    pour = CreativeOption.objects.get(kind="ad_template", key="pour-shot")
    pour.metadata = {
        **{k: v for k, v in pour.metadata.items() if k not in ("goal", "protect")},
        "goal": "trust",  # an operator's choice
        "motion_prompt": MIGRATION.OLD_TEXT["pour-shot"]["motion_prompt"],
    }
    pour.label = "Pour shot"
    pour.save(update_fields=["metadata", "label"])

    MIGRATION.forwards(apps, None)
    MIGRATION.forwards(apps, None)

    assert not CreativeOption.objects.get(kind="ad_template", key="lid-asmr").is_active
    own.refresh_from_db()
    assert own.is_active  # an operator's own row with the key stays
    pour.refresh_from_db()
    assert pour.metadata["goal"] == "trust"
    assert pour.metadata["protect"] == _spec("pour-shot")["metadata"]["protect"]
    assert pour.metadata["motion_prompt"] == _spec("pour-shot")["metadata"]["motion_prompt"]
    assert pour.label == _spec("pour-shot")["label"]


def test_the_migration_is_off_under_test_settings(settings: Any, catalog: None) -> None:  # noqa: F811
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = CreativeOption.objects.get(kind="ad_template", key="pour-shot")
    before = dict(row.metadata)
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata == before


def test_a_retired_key_is_a_400_like_any_key_out_of_the_catalogue(
    growth: Any,  # noqa: F811
    make_png_upload: Any,
) -> None:
    """A retired template is not in the gallery, and naming it is refused like
    any key the live catalogue does not hold (rule 18)."""
    from rest_framework.exceptions import ValidationError

    product = make_product(growth, make_png_upload)
    CreativeOption.objects.create(
        kind=CreativeKind.AD_TEMPLATE,
        key="lid-asmr",
        label="Lid lift ASMR",
        is_active=False,
        metadata=_spec("slow-unbox")["metadata"],
    )
    with pytest.raises(ValidationError):
        ad_templates.resolve(growth, "lid-asmr", product=product, with_person=False)
