"""X-01 — the pricing and billing pages read plan and pack copy in the
interface language (L-6).

English is each row's own `tagline` / `support_response`; French and Italian
are its `translations`, edited in admin like every other commercial word
(rule 10). A missing language or field reads English.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from django.apps import apps
from rest_framework.test import APIClient

from billing.models import Pack, PackKind, Plan

pytestmark = pytest.mark.django_db

SEED = import_module("billing.migrations.0035_plan_pack_translations")


def _plans(language: str) -> dict[str, dict[str, object]]:
    rows = APIClient().get("/api/v1/billing/plans/", HTTP_ACCEPT_LANGUAGE=language).json()
    return {row["code"]: row for row in rows}


def test_a_plan_reads_its_tagline_and_reply_time_in_the_page_language() -> None:
    Plan.objects.create(
        code="growth",
        display_name="Growth",
        tagline="Six posts a week.",
        support_response="1 working day",
        is_public=True,
        translations={"fr": {"tagline": "Six publications par semaine."}},
    )

    assert _plans("fr")["growth"]["tagline"] == "Six publications par semaine."
    # A field with no translation reads English, never blank.
    assert _plans("fr")["growth"]["support_response"] == "1 working day"
    assert _plans("it")["growth"]["tagline"] == "Six posts a week."
    assert _plans("en")["growth"]["tagline"] == "Six posts a week."


def test_a_pack_reads_its_tagline_in_the_page_language(auth_client: Any, workspace: Any) -> None:
    Pack.objects.create(
        code="videos-4",
        display_name="4 videos",
        tagline="Prepaid videos.",
        kind=PackKind.VIDEO,
        units=4,
        price_cents=1900,
        is_public=True,
        translations={"it": {"tagline": "Video prepagati."}},
    )
    rows = auth_client.get("/api/v1/billing/packs/", HTTP_ACCEPT_LANGUAGE="it").json()
    assert {row["code"]: row["tagline"] for row in rows}["videos-4"] == "Video prepagati."


def test_the_seed_translates_every_seeded_row_and_never_overwrites_an_edit() -> None:
    for words in [*SEED.PLANS.values(), *SEED.PACKS.values()]:
        assert set(words) == {"fr", "it"}
    Plan.objects.create(code="scale", display_name="Scale", tagline=SEED.SOURCE["scale"])
    retold = Plan.objects.create(code="spark", display_name="Spark", tagline="Our own words.")
    edited = Plan.objects.create(
        code="growth", display_name="Growth", translations={"fr": {"tagline": "Mine."}}
    )

    SEED.forwards(apps, None)

    assert Plan.objects.get(code="scale").translations["it"]["tagline"]
    edited.refresh_from_db()
    assert edited.translations == {"fr": {"tagline": "Mine."}}
    # Words an operator has retold are theirs to translate.
    retold.refresh_from_db()
    assert retold.translations == {}
