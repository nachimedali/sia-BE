"""S17-36 — claims and policy (`studio_content_audit.md` §4.8, §7).

* **The writer's words are linted** before they are kept: a guarantee, an
  absolute ("the best", "#1", "100%"), a cure or "clinically", a sentence that
  tells the viewer they have a trait (Meta's personal-attributes rule), or a
  claim the product has no proof on file for — and the template's own words
  come back, free (sales `legal-compliance-phrase-avoidance`, goose
  `claims-gate`, `ad-policy-check`).
* **The brand's own reviews**, each with the customer's consent recorded, and
  a proof card that quotes one. A review without consent is never shown.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps

from ai.ad_template_seed import TEMPLATES
from ai.models import CreativeOption
from ai.services import ad_templates, claims
from ai.tests.test_ad_scripts import answer, ask
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    GALLERY,
    catalog,
    flag_on,
    growth,
    make_product,
)
from billing.services import ledger

pytestmark = pytest.mark.django_db

REVIEW = {
    "quote": "The best oil in our kitchen.",
    "author": "Salma, Tunis",
    "where": "Instagram",
    "consent": True,
}


@pytest.fixture
def oil(growth: Any, make_png_upload: Any) -> Any:  # noqa: F811
    return make_product(growth, make_png_upload)


def reply(
    hook: str = "Weekday oil.", body: str = "Olive oil 500 ml, ready.", cta: str = "Try it."
) -> str:
    return f"HOOK: {hook}\nBODY: {body}\nCTA: {cta}"


# -----------------------------------------------------------------------------
# The lint
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Guaranteed to last.",
        "The best oil you'll ever buy.",
        "#1 in Tunisia.",
        "100% satisfaction.",
        "It cures dry skin.",
        "Clinically tested.",
        "Risk-free.",
        "Struggling with your acne?",
        "Le meilleur de la région.",
        "Garanti à vie.",
        "Il migliore del mercato.",
    ],
)
def test_risky_phrases_are_found(text: str) -> None:
    assert claims.risky(text), text


def test_plain_words_pass() -> None:
    assert claims.risky("Cold pressed, bottled in Sfax, ready for your salad.") == []


def test_a_risky_reply_gives_back_the_templates_own_words_free(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    answer(monkeypatch, reply(body="Olive oil 500 ml, guaranteed fresh."))
    before = ledger.credit_balance(oil.workspace)
    body = ask(auth_client, oil).json()
    assert body["by"] == "template" and body["credits"] == 0
    assert ledger.credit_balance(oil.workspace) == before


def test_an_unproven_claim_is_refused_and_a_proven_one_kept(
    auth_client: Any, oil: Any, monkeypatch: Any, make_png_upload: Any
) -> None:
    from content.services.media import ingest_media

    answer(monkeypatch, reply(body="Olive oil 500 ml, certified organic."))
    oil.claims = [{"key": "organic", "proof_media": None}]
    oil.save(update_fields=["claims"])
    assert claims.unproven("certified organic", oil) == ["organic"]
    assert ask(auth_client, oil).json()["by"] == "template"

    proof = ingest_media(workspace=oil.workspace, upload=make_png_upload("cert.png", (300, 300)))
    oil.claims = [{"key": "organic", "proof_media": proof.pk}]
    oil.save(update_fields=["claims"])
    assert claims.unproven("certified organic", oil) == []
    assert ask(auth_client, oil).json()["by"] == "model"


def test_a_risky_alternative_hook_is_dropped_but_the_script_kept(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    answer(monkeypatch, reply() + "\nHOOK2: The best oil, guaranteed.\nHOOK3: Sunday salad oil.")
    body = ask(auth_client, oil).json()
    assert body["by"] == "model"
    assert body["hooks"] == ["Weekday oil.", "Sunday salad oil."]


# -----------------------------------------------------------------------------
# Reviews and the proof card
# -----------------------------------------------------------------------------
def test_reviews_are_kept_with_their_consent_and_bounded(auth_client: Any, oil: Any) -> None:
    url = f"/api/v1/products/{oil.pk}/"
    ok = auth_client.patch(url, {"reviews": [REVIEW]}, format="json")
    assert ok.status_code == 200, ok.json()
    assert ok.json()["reviews"] == [REVIEW]
    for bad in (
        [{**REVIEW, "quote": "x" * 201}],
        [{**REVIEW, "consent": "yes"}],
        [{**REVIEW, "author": ""}],
        [REVIEW] * 11,
        "a review",
    ):
        assert auth_client.patch(url, {"reviews": bad}, format="json").status_code == 400, bad


def test_the_proof_card_needs_a_review_given_with_consent(auth_client: Any, oil: Any) -> None:
    def fit() -> Any:
        rows = auth_client.get(f"{GALLERY}?product={oil.pk}").json()["templates"]
        return next(row for row in rows if row["key"] == "proof-card")["fit"]

    assert fit() == "needs_reviews"
    oil.reviews = [{**REVIEW, "consent": False}]
    oil.save(update_fields=["reviews"])
    assert fit() == "needs_reviews"  # never without consent
    oil.reviews = [{**REVIEW, "consent": False}, REVIEW]
    oil.save(update_fields=["reviews"])
    assert fit() is None


def test_the_card_quotes_the_consented_review_and_names_its_author(oil: Any) -> None:
    oil.reviews = [{**REVIEW, "quote": "Not this one", "consent": False}, REVIEW]
    line = ad_templates.shot_line("“{review}”", oil, {"hook": "", "body": "", "cta": ""})
    author = ad_templates.shot_line("{review_author}", oil, {"hook": "", "body": "", "cta": ""})
    assert line == "“The best oil in our kitchen.”" and author == "Salma, Tunis"


def test_the_proof_card_is_seeded_and_its_migration_runs_once(settings: Any) -> None:
    assert any(spec["key"] == "proof-card" for spec in TEMPLATES)
    settings.SEED_CATALOG_ON_MIGRATE = True
    migration = importlib.import_module("ai.migrations.0045_proof_card")
    migration.forwards(apps, None)
    migration.forwards(apps, None)
    assert CreativeOption.objects.filter(kind="ad_template", key="proof-card").count() == 1
