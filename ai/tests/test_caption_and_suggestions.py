"""Caption-from-media, hashtags and post suggestions (P1-13).

Three additions, and they are deliberately **not** the same shape:

* **`CAPTION`** is generative and vision-backed. It goes through the whole
  pipeline — DB-only half in-request, provider half on `ai_q`, quality gate,
  debit on pass only — because it calls a provider and costs money.
* **`SUGGEST`** is generative too, grounded in the workspace's own
  top-percentile posts. Same pipeline, same discipline.
* **Hashtags are not.** They are counted in code against the category corpus,
  because the useful answer is *which tags are actually working in this
  vertical*, and a model inventing plausible ones is worse than useless. A
  database aggregate has nothing for a quality gate to judge and nothing to
  charge for, so it charges nothing.

That last one is a deliberate departure from BUILD-PLAN Phase 1's "all go
through the existing pipeline", recorded on the task rather than made quietly.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model

from ai.models import Generation, GenerationKind, GenerationMode, GenerationStatus
from ai.services import hashtags
from ai.services.costing import resolve_cost
from ai.services.pipeline import (
    GenerationModeNotAvailableError,
    create_generation,
    run_generation,
)
from billing.services import ledger
from content.services.media import ingest_media
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

GENERATE_URL = "/api/v1/ai/generate/"
HASHTAGS_URL = "/api/v1/ai/hashtags/"


@pytest.fixture
def credited(workspace: Any, generation_costs: Any) -> Any:
    """`ai/tests/conftest.py` already funds the workspace fixture; this only
    adds the seeded cost table, without which `resolve_cost` refuses by
    design."""
    return workspace


@pytest.fixture
def categorised(workspace: Any, category: Any) -> Any:
    """The corpus is shared per *category* (D11), so a workspace only sees one
    if it has been placed in a vertical."""
    workspace.category = category
    workspace.save(update_fields=["category"])
    return workspace


# -----------------------------------------------------------------------------
# CAPTION
# -----------------------------------------------------------------------------
def test_caption_needs_a_media_asset(credited: Any, user: Any) -> None:
    """The whole input. A caption mode with no image is a text generation
    wearing the wrong name."""
    with pytest.raises(GenerationModeNotAvailableError):
        create_generation(
            workspace=credited,
            user=user,
            kind=GenerationKind.TEXT,
            mode=GenerationMode.CAPTION,
            prompt="Describe this",
        )


def test_caption_records_the_media_it_read(credited: Any, user: Any, media_asset: Any) -> None:
    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.CAPTION,
        prompt="Warm and short",
        source_media=media_asset,
    )
    assert generation.source_media_id == media_asset.pk


def test_caption_runs_through_the_pipeline_and_produces_variants(
    credited: Any, user: Any, media_asset: Any
) -> None:
    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.CAPTION,
        prompt="Warm and short",
        source_media=media_asset,
    )
    run_generation(generation, n=2)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    assert generation.variants.count() == 2
    assert all(variant.body for variant in generation.variants.all())


def test_caption_reads_the_image_not_only_the_prompt(
    credited: Any, user: Any, media_asset: Any, text_provider: Any
) -> None:
    """A "vision" capability that never opened the file would pass every test
    above while producing captions of nothing."""
    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.CAPTION,
        prompt="Warm and short",
        source_media=media_asset,
    )
    run_generation(generation, n=1)

    assert text_provider.captions, "the vision capability was never called"
    assert text_provider.captions[-1]["image_bytes"], "the image was not read"


def test_caption_debits_its_cost_once_on_success(
    credited: Any, user: Any, media_asset: Any
) -> None:
    before = ledger.credit_balance(credited)
    cost = resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.CAPTION)

    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.CAPTION,
        prompt="Warm and short",
        source_media=media_asset,
    )
    run_generation(generation, n=1)

    assert ledger.credit_balance(credited) == before - cost


def test_caption_has_a_seeded_cost_row(generation_costs: Any) -> None:
    """A missing row is a hard error by design (`resolve_cost`), so a mode
    without one cannot be generated at all."""
    assert resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.CAPTION) > 0


def test_caption_refuses_another_workspaces_media(
    credited: Any, user: Any, make_png_upload: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="stranger@example.com", password="x")
    theirs = provision_workspace(stranger, name="Someone Else")
    foreign = ingest_media(workspace=theirs, upload=make_png_upload("theirs.png"))

    with pytest.raises(GenerationModeNotAvailableError):
        create_generation(
            workspace=credited,
            user=user,
            kind=GenerationKind.TEXT,
            mode=GenerationMode.CAPTION,
            prompt="Describe this",
            source_media=foreign,
        )


def test_caption_over_the_api(auth_client: Any, credited: Any, media_asset: Any) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {
            "kind": GenerationKind.TEXT,
            "mode": GenerationMode.CAPTION,
            "prompt": "Warm and short",
            "source_media": media_asset.pk,
            "n": 1,
        },
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["status"] == GenerationStatus.SUCCEEDED


def test_caption_over_the_api_without_media_is_400(auth_client: Any, credited: Any) -> None:
    response = auth_client.post(
        GENERATE_URL,
        {"kind": GenerationKind.TEXT, "mode": GenerationMode.CAPTION, "prompt": "x", "n": 1},
        format="json",
    )
    assert response.status_code == 400


def test_a_caption_of_a_video_is_refused(credited: Any, user: Any) -> None:
    """The vision capability reads still images. A video would be read as
    bytes it cannot decode, and the honest answer is a refusal."""
    from content.models import MediaAsset

    video = MediaAsset.objects.create(
        workspace=credited, kind="VIDEO", mime="video/mp4", duration_ms=4000
    )
    with pytest.raises(GenerationModeNotAvailableError):
        create_generation(
            workspace=credited,
            user=user,
            kind=GenerationKind.TEXT,
            mode=GenerationMode.CAPTION,
            prompt="x",
            source_media=video,
        )


# -----------------------------------------------------------------------------
# SUGGEST
# -----------------------------------------------------------------------------
def test_suggest_is_creatable_and_grounded(credited: Any, user: Any) -> None:
    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.SUGGEST,
        prompt="Three ideas for next week",
    )
    run_generation(generation, n=3)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    assert generation.variants.count() == 3


def test_suggest_has_a_seeded_cost_row(generation_costs: Any) -> None:
    assert resolve_cost(kind=GenerationKind.TEXT, mode=GenerationMode.SUGGEST) > 0


def test_suggest_falls_back_gracefully_with_no_history(credited: Any, user: Any) -> None:
    """A brand-new workspace has no top-percentile post to lean on. Grounding
    is opportunistic everywhere else in this module and stays so here — no
    history generates exactly as an ungrounded prompt would, rather than
    failing."""
    generation = create_generation(
        workspace=credited,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.SUGGEST,
        prompt="Ideas",
    )
    run_generation(generation, n=1)
    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED


# -----------------------------------------------------------------------------
# Hashtags — counted, not invented
# -----------------------------------------------------------------------------
def test_hashtags_are_ranked_by_how_often_the_category_uses_them(
    categorised: Any, trend_corpus: Any
) -> None:
    ranked = hashtags.rank_for_workspace(categorised)

    assert [row.tag for row in ranked[:2]] == ["ceramics", "handmade"]
    assert ranked[0].count > ranked[1].count


def test_hashtags_report_the_count_they_were_ranked_on(categorised: Any, trend_corpus: Any) -> None:
    """The number is the evidence. A ranked list with no counts is an opinion
    the user cannot check."""
    ranked = hashtags.rank_for_workspace(categorised)
    assert all(row.count >= 1 for row in ranked)


def test_a_workspace_with_no_category_gets_nothing_rather_than_noise(
    categorised: Any, trend_corpus: Any
) -> None:
    categorised.category = None
    categorised.save(update_fields=["category"])
    assert hashtags.rank_for_workspace(categorised) == []


def test_hashtags_cost_no_credits(
    auth_client: Any, categorised: Any, trend_corpus: Any, generation_costs: Any
) -> None:
    """Counted in code, not generated. There is nothing to charge for and
    nothing for a quality gate to judge."""
    before = ledger.credit_balance(categorised)

    response = auth_client.get(HASHTAGS_URL)
    assert response.status_code == 200
    assert ledger.credit_balance(categorised) == before


def test_hashtags_over_the_api(auth_client: Any, categorised: Any, trend_corpus: Any) -> None:
    body = auth_client.get(HASHTAGS_URL).json()
    assert body["hashtags"][0]["tag"] == "ceramics"
    assert body["hashtags"][0]["count"] >= 1


def test_hashtags_never_leak_another_categorys_corpus(categorised: Any, trend_corpus: Any) -> None:
    """The corpus is shared per category (D11), not per workspace — which is
    exactly why the category filter has to be right."""
    from categories.models import Category

    other = Category.objects.create(name="Fitness", slug="fitness")
    categorised.category = other
    categorised.save(update_fields=["category"])

    assert hashtags.rank_for_workspace(categorised) == []


# -----------------------------------------------------------------------------
# Mode gating
# -----------------------------------------------------------------------------
def test_the_new_modes_are_directly_creatable() -> None:
    from ai.services.pipeline import DIRECTLY_CREATABLE_MODES

    assert GenerationMode.CAPTION in DIRECTLY_CREATABLE_MODES
    assert GenerationMode.SUGGEST in DIRECTLY_CREATABLE_MODES


def test_still_unbuilt_modes_stay_refused(credited: Any, user: Any) -> None:
    """TREND, REPURPOSE and RECIPE are Phase 5 and Phase 14's. Adding two modes
    must not have widened the gate to all of them."""
    for mode in (GenerationMode.TREND, GenerationMode.REPURPOSE, GenerationMode.RECIPE):
        with pytest.raises(GenerationModeNotAvailableError):
            create_generation(
                workspace=credited,
                user=user,
                kind=GenerationKind.TEXT,
                mode=mode,
                prompt="x",
            )


def test_a_caption_generation_is_scoped_to_its_workspace(
    auth_client: Any, credited: Any, media_asset: Any
) -> None:
    stranger = get_user_model().objects.create_user(email="outside@example.com", password="x")
    theirs = provision_workspace(stranger, name="Another Company")
    foreign = Generation.objects.create(
        workspace=theirs,
        user=stranger,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.CAPTION,
        prompt="not yours",
    )
    assert auth_client.get(f"/api/v1/ai/generations/{foreign.pk}/").status_code == 404
