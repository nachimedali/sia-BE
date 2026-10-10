"""The crawler's guards, the fake web, and the checks on inferred values."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from brand.fetching import FakeSiteFetcher, UnsafeUrlError, assert_public_url
from brand.inference import LLMBrandInference, validate


# --- SSRF guard -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "ftp://example.com/",
        "https://user:secret@example.com/",
        "https://example.com:8080/",
        "file:///etc/passwd",
    ],
)
def test_the_crawler_refuses_anything_that_is_not_a_public_web_address(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url(url)


def test_a_public_address_is_allowed() -> None:
    assert_public_url("https://8.8.8.8/")


# --- the fake web -----------------------------------------------------------------------
def test_the_fake_serves_fixture_pages_and_404s_the_rest() -> None:
    web = FakeSiteFetcher()

    home = web.fetch("https://www.djerbaolive.tn/")
    missing = web.fetch("https://djerbaolive.tn/nothing-here")

    assert home is not None and home.ok and "Djerba Olive Co." in home.text
    assert missing is not None and missing.status == 404
    assert web.fetch("https://no-such-site.example/") is None


def test_the_fake_cannot_be_walked_out_of_its_fixture_folder() -> None:
    page = FakeSiteFetcher().fetch("https://djerbaolive.tn/../../fetching.py")
    assert page is None or page.status == 404


# --- inference checks --------------------------------------------------------------------
TEXT = "Pressed the same day, from trees older than our grandparents. Made for home cooks."


def test_a_quote_must_appear_verbatim_in_the_site_text() -> None:
    kept = validate(
        {"voice": {"quote": "Pressed the same day, from trees older than our grandparents."}},
        text=TEXT,
        categories=[],
    )
    dropped = validate(
        {"voice": {"quote": "We are the finest oil in the world."}}, text=TEXT, categories=[]
    )

    assert kept["voice"]["quote"].startswith("Pressed the same day")
    assert "voice" not in dropped


def test_choices_must_be_known_and_competitors_are_only_ever_inferred() -> None:
    found = validate(
        {
            "voice": {"preset": "LOUD", "descriptors": ["Warm", " ", "Rooted"]},
            "category": "food & drink",
            "business_type": "MEGACORP",
            "competitors": [
                {"name": "Zitouna Gold", "domain": "https://zitounagold.tn/", "why": "Same story"},
                {"name": "", "domain": "x.tn"},
                {"name": "Odd", "domain": "not a domain"},
            ],
        },
        text=TEXT,
        categories=["Food & Drink", "Fashion"],
    )

    assert found["voice"] == {"descriptors": ["warm", "rooted"]}
    assert found["category"] == "Food & Drink"
    assert "business_type" not in found
    assert found["competitors"] == [
        {
            "name": "Zitouna Gold",
            "domain": "zitounagold.tn",
            "why": "Same story",
            "relationship": "inferred",
        },
        {"name": "Odd", "domain": "", "why": "", "relationship": "inferred"},
    ]


def test_the_llm_answer_is_parsed_from_a_fenced_block_then_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "audience": {"summary": "Home cooks who care.", "segments": ["Home cooks"]},
        "category": "Nope",
    }
    body = f"```json\n{json.dumps(answer)}\n```"

    class Provider:
        def generate(self, **_: Any) -> Any:
            return SimpleNamespace(variants=[SimpleNamespace(body=body)])

    monkeypatch.setattr("ai.providers.llm_text.get_text_provider", lambda: Provider())

    found = LLMBrandInference().infer(name="X", text=TEXT, categories=["Food"], products=[])

    assert found == {"audience": {"summary": "Home cooks who care.", "segments": ["Home cooks"]}}


def test_a_provider_failure_leaves_the_inferred_sections_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def generate(self, **_: Any) -> Any:
            raise RuntimeError("gateway down")

    monkeypatch.setattr("ai.providers.llm_text.get_text_provider", lambda: Broken())

    assert LLMBrandInference().infer(name="X", text=TEXT, categories=[], products=[]) == {}
