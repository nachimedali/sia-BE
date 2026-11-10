"""S17-30 — the person identity lock (`studio_content_audit.md` §4.4).

A face described by age, look, style and mood alone comes out as a composite
that drifts from shot to shot (goose-skills `create-creator-takes-h3`). The
invented person is now described by gender, age, heritage, build, hair, a whole
outfit (top, bottom, what they wear with it) and a calm face unless asked
otherwise — and that sentence is written **once**, when the avatar is saved,
then sent word for word with every render.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command

from ai.ad_template_seed import AVATAR_TRAITS
from ai.models import CreativeKind, CreativeOption
from billing.models import FeatureFlag
from billing.services import ledger
from billing.services.flags import AD_TEMPLATES_S17
from brand.models import Avatar, AvatarKind
from brand.services import avatars
from content.services.media import ingest_media

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ai.migrations.0039_avatar_identity_traits")


@pytest.fixture
def traits(db: None, generation_costs: None) -> None:
    call_command("seed_creative_options", verbosity=0)


def _group(name: str) -> list[dict[str, Any]]:
    return [row for row in AVATAR_TRAITS if row["metadata"]["group"] == name]


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_gender_heritage_and_hair_are_groups_of_their_own() -> None:
    assert [row["key"] for row in _group("gender")] == ["gender-woman", "gender-man"]
    assert len(_group("heritage")) >= 8
    assert len(_group("hair")) >= 6
    assert set(avatars.GROUPS) >= {"gender", "heritage", "hair"}


def test_every_outfit_names_a_top_a_bottom_and_what_goes_with_it() -> None:
    for row in _group("style"):
        meta = row["metadata"]
        for part in ("top", "bottom", "accessories"):
            assert meta[part] and meta[part] in row["prompt_fragment"], (row["key"], part)


def test_archetypes_never_choose_a_gender_or_a_heritage() -> None:
    """A quick start sets a role, never who the person is."""
    for row in _group("archetype"):
        assert not {"gender", "heritage"} & set(row["metadata"]["values"]), row["key"]


# -----------------------------------------------------------------------------
# The sentence
# -----------------------------------------------------------------------------
def test_the_sentence_says_who_in_a_fixed_order_and_is_calm_by_default(traits: None) -> None:
    sentence = avatars.describe(
        traits={
            "gender": "gender-woman",
            "age": "age-30s",
            "heritage": "heritage-north-african",
            "hair": "hair-long-dark-wavy",
            "style": "style-casual",
        }
    )
    rows = {
        row.key: row.prompt_fragment for row in CreativeOption.objects.filter(kind="avatar_trait")
    }
    order = [
        "gender-woman",
        "age-30s",
        "heritage-north-african",
        "hair-long-dark-wavy",
        "style-casual",
    ]
    positions = [sentence.index(rows[key]) for key in order]
    assert positions == sorted(positions)
    assert avatars.CALM in sentence  # no energy chosen: calm, never a constant grin


def test_unknown_values_in_the_new_groups_are_400(traits: None) -> None:
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        avatars.clean_traits({"gender": "gender-robot"})
    with pytest.raises(ValidationError):
        avatars.clean_traits({"heritage": "age-30s"})  # a key from another group


def test_the_sentence_is_written_once_when_the_avatar_is_saved(
    auth_client: Any, workspace: Any, plans: dict[str, Any], traits: None
) -> None:
    FeatureFlag.objects.create(organization=None, key=AD_TEMPLATES_S17, enabled=True)
    workspace.organization.plan = plans["growth"]
    workspace.organization.save(update_fields=["plan"])
    ledger.grant_credits(workspace, 100, note="test")
    chosen = {"gender": "gender-man", "age": "age-40s", "heritage": "heritage-southern-european"}
    draft = auth_client.post("/api/v1/brand/avatars/drafts/", {"traits": chosen}, format="json")
    assert draft.status_code == 202, draft.json()
    saved = auth_client.post(
        "/api/v1/brand/avatars/",
        {
            "kind": "synthetic",
            "draft": draft.json()["id"],
            "index": 0,
            "name": "Sami",
            "languages": ["fr"],
        },
        format="json",
    )
    assert saved.status_code == 201, saved.json()
    avatar = Avatar.objects.get(pk=saved.json()["id"])
    written = avatar.identity
    assert written == avatars.describe(traits=avatar.traits)

    CreativeOption.objects.filter(kind="avatar_trait", key="age-40s").update(
        prompt_fragment="changed in admin later"
    )
    assert avatars.describe(avatar) == written  # the saved person never drifts


def test_a_person_saved_before_the_lock_is_described_from_their_traits(
    workspace: Any, make_png_upload: Any, traits: None
) -> None:
    portrait = ingest_media(workspace=workspace, upload=make_png_upload("f.png", (512, 512)))
    old = Avatar.objects.create(
        workspace=workspace,
        name="Old",
        kind=AvatarKind.SYNTHETIC,
        portrait=portrait,
        traits={"age": "age-30s"},
        languages=["en"],
    )
    assert old.identity == ""
    assert avatars.describe(old) == avatars.describe(traits=old.traits)


# -----------------------------------------------------------------------------
# The migration
# -----------------------------------------------------------------------------
def test_the_migration_adds_the_groups_and_rewrites_untouched_outfits(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    casual = CreativeOption.objects.create(
        kind=CreativeKind.AVATAR_TRAIT,
        key="style-casual",
        label="casual",
        prompt_fragment=MIGRATION.OLD_FRAGMENTS["style-casual"],
        metadata={"group": "style", "seed": "v2"},
    )
    smart = CreativeOption.objects.create(
        kind=CreativeKind.AVATAR_TRAIT,
        key="style-smart",
        label="smart",
        prompt_fragment="our own words",
        metadata={"group": "style", "seed": "v2"},
    )

    MIGRATION.forwards(apps, None)
    MIGRATION.forwards(apps, None)

    casual.refresh_from_db()
    smart.refresh_from_db()
    new = next(row for row in AVATAR_TRAITS if row["key"] == "style-casual")
    assert casual.prompt_fragment == new["prompt_fragment"]
    assert casual.metadata["top"] == new["metadata"]["top"]
    assert smart.prompt_fragment == "our own words"
    assert CreativeOption.objects.filter(kind="avatar_trait", key="gender-woman").exists()
    assert CreativeOption.objects.get(kind="avatar_trait", key="gender-woman").translations["fr"]
