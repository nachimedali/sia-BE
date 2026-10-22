"""Rephrasing the Studio brief before it reaches the image or video model.

The Studio's choices arrive as labelled fragments ("Setting: …", "Lighting: …").
The text model (NVIDIA) rewrites them into one art-director brief, following
the goose-skills creative rules (product-photoshoot, animate-image,
create-image-fal, create-video-fal). What these tests pin:

* the rewrite is what the image/video model is told, and it is kept on the row;
* the hard parts never go through the model: the fidelity prefix, the product's
  restrictions and the "keep out of the frame" line are appended in code,
  verbatim, after the rewrite;
* a rewrite that invents a number, comes back empty or fails falls back to the
  plain template brief — the generation still runs;
* it runs once per generation, only for Studio runs, and only with the flag on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import call_command

from ai.models import GenerationKind, GenerationMode, GenerationStatus
from ai.prompts.image_v1 import PROMPT_PREFIX
from ai.providers.fake import _fake_image_provider
from ai.services import creative, rephrase
from ai.services.pipeline import create_generation, run_generation
from billing.models import FeatureFlag
from billing.services.flags import STUDIO_REPHRASE
from billing.services.ledger import grant_credits
from common.exceptions import ProviderError

pytestmark = pytest.mark.django_db

BRIEF = {
    "scene": "beach-shoreline",
    "light": "golden-hour",
    "camera": "low-hero",
    "moods": ["editorial"],
    "avoid": "plastic, stock logos",
}
REWRITE = (
    "The mug stands on wet sand at the shoreline, caught low and close in warm "
    "golden-hour light, long soft shadows behind it, an editorial calm."
)


class Scripted:
    """A text model that answers with a fixed reply and records what it was asked."""

    def __init__(self, reply: str = REWRITE, *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        self.calls.append({"system": system, "prompt": prompt})
        if self.error:
            raise self.error
        return SimpleNamespace(
            variants=[SimpleNamespace(body=self.reply)], model="meta/muse-glimmer-30b"
        )


@pytest.fixture
def studio(workspace: Any, plans: Any, generation_costs: None) -> Any:
    call_command("seed_creative_options", verbosity=0)
    workspace.organization.plan = plans["pro"]
    workspace.organization.save(update_fields=["plan", "updated_at"])
    grant_credits(workspace, 500, note="test funding")
    return workspace


@pytest.fixture
def mug(product_with_reference_image: Any) -> Any:
    product_with_reference_image.restrictions = ["never show the mug empty"]
    product_with_reference_image.save(update_fields=["restrictions"])
    return product_with_reference_image


def _studio_run(workspace: Any, user: Any, product: Any, brief: dict[str, Any] = BRIEF) -> Any:
    row = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug on the beach",
        product=product,
        creative=creative.normalize(brief),
    )
    return run_generation(row, n=1)


def _script(monkeypatch: pytest.MonkeyPatch, model: Scripted) -> Scripted:
    monkeypatch.setattr(rephrase, "get_text_provider", lambda *_a, **_k: model)
    return model


# --- images ---------------------------------------------------------------------------
def test_the_image_model_is_told_the_rewrite_and_the_row_keeps_it(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _script(monkeypatch, Scripted())

    done = _studio_run(studio, user, mug)

    assert done.status == GenerationStatus.SUCCEEDED, done.error_detail
    sent = _fake_image_provider.calls[-1]["prompt"]
    assert REWRITE in sent
    assert done.rephrased_prompt == REWRITE
    assert done.rephrased_by == "meta/muse-glimmer-30b"
    # What the model was given: the idea and every choice, as labelled lines.
    asked = model.calls[0]["prompt"]
    assert "the mug on the beach" in asked
    assert "Setting:" in asked and "Lighting:" in asked


def test_fidelity_restrictions_and_exclusions_are_appended_in_code_verbatim(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model rewrites the look; it never gets to rewrite the rules."""
    _script(monkeypatch, Scripted())

    _studio_run(studio, user, mug)

    sent = _fake_image_provider.calls[-1]["prompt"]
    assert sent.startswith(PROMPT_PREFIX)
    assert "never show the mug empty" in sent
    assert "Keep out of the frame: plastic, stock logos." in sent
    assert sent.index(REWRITE) < sent.index("Keep out of the frame")


def test_the_instructions_carry_the_goose_creative_rules(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _script(monkeypatch, Scripted())

    _studio_run(studio, user, mug)

    system = model.calls[0]["system"].lower()
    assert "do not add a choice" in system
    assert "never redesign" in system
    assert "never name a phone or camera" in system
    assert "prompt only" in system


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        REWRITE + " Rated 4.9 by 12000 buyers.",  # a number the brief never gave
        "Visit https://example.com for the look.",
        "x" * 2001,
    ],
)
def test_a_rewrite_that_fails_its_checks_falls_back_to_the_template(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    _script(monkeypatch, Scripted(reply))

    done = _studio_run(studio, user, mug)

    assert done.status == GenerationStatus.SUCCEEDED
    assert done.rephrased_by == "template"
    sent = _fake_image_provider.calls[-1]["prompt"]
    assert "the mug on the beach" in sent and "Setting:" in sent
    assert "12000" not in sent and "example.com" not in sent


def test_a_model_outage_never_fails_the_generation(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, Scripted(error=ProviderError("gateway down")))

    done = _studio_run(studio, user, mug)

    assert done.status == GenerationStatus.SUCCEEDED
    assert done.rephrased_by == "template"


def test_it_rephrases_once_per_generation_not_once_per_attempt(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected attempt is retried with the same brief, so the brief is
    written once."""
    model = _script(monkeypatch, Scripted())
    from ai.models import QualityGateConfig

    config = QualityGateConfig.get_solo()
    config.identity_similarity_threshold = 1.01  # nothing passes: every attempt runs
    config.save()

    _studio_run(studio, user, mug)

    assert len(model.calls) == 1
    assert len(_fake_image_provider.calls) == config.max_regeneration_attempts


def test_a_run_with_no_studio_brief_is_not_rephrased(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autopilot and every other non-Studio caller: no extra call, prompt as before."""
    model = _script(monkeypatch, Scripted())
    row = create_generation(
        workspace=studio,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug",
        product=mug,
    )

    done = run_generation(row, n=1)

    assert model.calls == []
    assert done.rephrased_prompt == "" and done.rephrased_by == ""


def test_flag_off_sends_the_brief_exactly_as_before(
    studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _script(monkeypatch, Scripted())
    FeatureFlag.objects.create(organization=studio.organization, key=STUDIO_REPHRASE, enabled=False)

    done = _studio_run(studio, user, mug)

    assert model.calls == []
    assert done.rephrased_by == ""
    sent = _fake_image_provider.calls[-1]["prompt"]
    assert REWRITE not in sent and "Setting:" in sent


def test_the_fake_text_model_rewrites_deterministically_and_keeps_every_choice(
    studio: Any, user: Any, mug: Any
) -> None:
    """A fresh checkout (no key) still goes through the rephrase step."""
    done = _studio_run(studio, user, mug)

    assert done.rephrased_by == "fake-text-1"
    assert "Setting:" not in done.rephrased_prompt
    assert "shoreline" in done.rephrased_prompt.lower()


def test_the_api_shows_the_brief_as_it_was_rewritten(
    auth_client: Any, studio: Any, user: Any, mug: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, Scripted())
    done = _studio_run(studio, user, mug)

    body = auth_client.get(f"/api/v1/ai/generations/{done.pk}/").json()

    assert body["rephrased_prompt"] == REWRITE
    assert body["rephrased_by"] == "meta/muse-glimmer-30b"
