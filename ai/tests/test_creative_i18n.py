"""Creative options speak the interface language (L-6): `Accept-Language`
picks `translations[<lang>]`, anything unknown or missing reads English."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.management import call_command

from ai.creative_i18n import TRANSLATIONS
from ai.models import CreativeOption

URL = "/api/v1/ai/creative-options/"


@pytest.fixture
def seeded(db: Any) -> None:
    call_command("seed_creative_options", verbosity=0)


def camera_label(client: Any, lang: str | None) -> str:
    headers = {"HTTP_ACCEPT_LANGUAGE": lang} if lang else {}
    body = client.get(URL, **headers).json()
    return next(r for r in body["options"]["camera"] if r["key"] == "eye-level")["label"]


def test_a_known_language_gets_its_translation(
    auth_client: Any, workspace: Any, seeded: None, generation_costs: None
) -> None:
    assert camera_label(auth_client, "fr") == TRANSLATIONS["camera|eye-level"][0]
    assert camera_label(auth_client, "it") == TRANSLATIONS["camera|eye-level"][2]


@pytest.mark.parametrize("lang", [None, "en", "de", "fr;q=oops"])
def test_english_unknown_and_absent_languages_read_english(
    auth_client: Any, workspace: Any, seeded: None, generation_costs: None, lang: str | None
) -> None:
    assert camera_label(auth_client, lang) == "Eye-level hero"


def test_a_row_without_a_translation_falls_back_to_english(
    auth_client: Any, workspace: Any, seeded: None, generation_costs: None
) -> None:
    CreativeOption.objects.filter(kind="camera", key="eye-level").update(translations={})
    assert camera_label(auth_client, "fr") == "Eye-level hero"


def test_every_seeded_row_is_translated_in_both_languages(seeded: None) -> None:
    for row in CreativeOption.objects.all():
        for lang in ("fr", "it"):
            entry = row.translations.get(lang, {})
            assert entry.get("label"), f"{row.kind}|{row.key} has no {lang} label"
            if row.description:
                assert entry.get("description"), f"{row.kind}|{row.key} has no {lang} description"


def test_ad_templates_and_their_lengths_are_translated_too(db: Any) -> None:
    from django.utils import translation

    row = CreativeOption(
        kind="ad_template", key="x", label="Slow", translations={"fr": {"label": "Lent"}}
    )
    with translation.override("fr"):
        assert row.localized("label") == "Lent"
        assert row.localized("description") == ""
    with translation.override("it"):
        assert row.localized("label") == "Slow"
