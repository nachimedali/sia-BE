"""The text model fills what the product page's markup did not say (S1).

The rules read first — the shop's JSON, JSON-LD, OpenGraph. Only the fields they
left empty (short description, long description, features) are asked of the
model, and **every value it returns must appear in the page's own text**: the
model picks sentences off the page, it never writes them. A field it fills is
labelled `confidence: "inferred"` so the form can say so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from billing.models import FeatureFlag
from billing.services.flags import PRODUCT_SCAN_ASSIST
from brand.fetching import FakeSiteFetcher, set_override
from brand.inference import LLMBrandInference
from brand.models import ImportStatus

pytestmark = pytest.mark.django_db

IMPORTS = "/api/v1/brand/product-imports/"
APRON = "plainstudio.com/shop/canvas-apron"
PAGE = "https://plainstudio.com/shop/canvas-apron"
LONG = (
    "Cut from heavy waxed canvas, this apron is made for long shifts at the counter. "
    "It ties at the back with cotton straps and wipes clean with a damp cloth."
)


class Model:
    def __init__(self, answer: Any = None, *, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        self.calls.append({"system": system, "prompt": prompt})
        if self.error:
            raise self.error
        body = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(variants=[SimpleNamespace(body=body)])


@pytest.fixture
def web() -> Any:
    fake = FakeSiteFetcher()
    set_override(fake)
    yield fake
    set_override(None)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Model:
    scripted = Model(
        {
            "short": LONG.split(". ")[0] + ".",
            "long": LONG,
            "features": [
                "two deep front pockets",
                "an adjustable neck strap",
                "brass rivets at the stress points",
            ],
        }
    )
    monkeypatch.setattr("ai.providers.llm_text.get_text_provider", lambda *_a, **_k: scripted)
    monkeypatch.setattr(
        "brand.services.product_imports.get_brand_inference", lambda: LLMBrandInference()
    )
    return scripted


def _import(client: Any, url: str = APRON) -> dict[str, Any]:
    response = client.post(IMPORTS, {"url": url}, format="json")
    assert response.status_code == 201, response.content
    body: dict[str, Any] = response.json()
    return body


def test_the_model_fills_the_gaps_with_sentences_from_the_page(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    body = _import(auth_client)

    assert body["status"] == ImportStatus.SUCCEEDED
    result = body["result"]
    assert result["long"] == {"value": LONG, "source": PAGE, "confidence": "inferred"}
    assert result["short"]["value"].startswith("Cut from heavy waxed canvas")
    assert result["features"]["value"] == [
        "two deep front pockets",
        "an adjustable neck strap",
        "brass rivets at the stress points",
    ]
    # The rules' own fields are untouched.
    assert result["name"]["confidence"] == "high"
    # It was told which fields to fill and given the page text, nothing else.
    asked = json.loads(model.calls[0]["prompt"])
    assert sorted(asked["fill"]) == ["features", "long", "short"]
    assert "two deep front pockets" in asked["page_text"]


def test_anything_not_on_the_page_is_dropped(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    model.answer = {
        "short": "The best apron money can buy, loved by chefs.",
        "long": "Handmade in Lyon from organic canvas.",
        "features": ["two deep front pockets", "lifetime warranty", 42],
    }

    result = _import(auth_client)["result"]

    assert "short" not in result and "long" not in result
    assert result["features"]["value"] == ["two deep front pockets"]


def test_a_page_the_rules_fully_read_is_never_sent_to_the_model(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    body = _import(auth_client, "djerbaolive.tn/products/first-harvest")

    assert body["status"] == ImportStatus.SUCCEEDED
    assert model.calls == []


def test_a_model_outage_leaves_the_import_as_the_rules_found_it(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    model.error = RuntimeError("gateway down")

    body = _import(auth_client)

    assert body["status"] == ImportStatus.SUCCEEDED
    assert body["result"]["name"]["value"] == "Canvas apron"
    assert not {"short", "long", "features"} & set(body["result"])


def test_a_reply_that_is_not_json_fills_nothing(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    model.answer = "Sorry, I can't help with that."

    result = _import(auth_client)["result"]

    assert not {"short", "long", "features"} & set(result)


def test_flag_off_reads_with_the_rules_only(
    auth_client: Any, workspace: Any, web: Any, model: Model
) -> None:
    FeatureFlag.objects.create(
        organization=workspace.organization, key=PRODUCT_SCAN_ASSIST, enabled=False
    )

    result = _import(auth_client)["result"]

    assert model.calls == []
    assert "long" not in result


def test_the_fake_fills_the_description_from_the_page_with_no_key(
    auth_client: Any, workspace: Any, web: Any
) -> None:
    """A fresh checkout runs the same step against the fake."""
    result = _import(auth_client)["result"]

    assert result["long"]["value"] == LONG
    assert result["long"]["confidence"] == "inferred"
    assert result["short"]["value"] and result["short"]["value"] in LONG
