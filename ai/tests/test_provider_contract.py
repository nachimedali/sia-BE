"""`TextProvider`/`ImageProvider` contract (design.md §9, A8).

One suite, both implementations where it makes sense. The fake runs
everywhere; the real adapters run against `httpx.MockTransport` so their
**parameter assembly** is covered without credentials — that is where the
bugs actually are (a misnamed field, a missing header), and none of them
would be caught by testing the fake alone. Mirrors
`billing/tests/test_gateway_contract.py`'s shape, adapted to `httpx` instead
of the `stripe` SDK's own stub style.

A run against live credentials is marked `integration` and skipped without
them (design.md §9).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from django.test import override_settings

from ai.providers.fake import FakeImageProvider, FakeTextProvider
from ai.providers.llm_text import LLMTextProvider, get_text_provider
from ai.providers.nanobanana_image import NanoBananaImageProvider, get_image_provider


def _chat_completion(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }


def _patch_transport(monkeypatch: Any, handler: Any) -> None:
    """Swaps only the network transport, leaving `_client()`'s own logic —
    base URL, auth header, timeout — running for real. Patching `_client()`
    itself, instead, would bypass exactly the parameter assembly this
    contract test exists to cover."""
    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


@pytest.fixture
def text_transport(monkeypatch: Any) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_completion("a cosy morning, remastered"))

    _patch_transport(monkeypatch, handler)
    return captured


@pytest.fixture
def image_transport(monkeypatch: Any) -> list[httpx.Request]:
    captured: list[httpx.Request] = []
    png_1x1 = base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6360000002000100"
            "00000000000000000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith(":batchGenerateContent"):
            return httpx.Response(200, json={"name": "batches/1"})
        if "/batches/1" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inline_data": {
                                                "mime_type": "image/png",
                                                "data": png_1x1,
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"inline_data": {"mime_type": "image/png", "data": png_1x1}}]
                        }
                    }
                ]
            },
        )

    _patch_transport(monkeypatch, handler)
    return captured


# -----------------------------------------------------------------------------
# TextProvider
# -----------------------------------------------------------------------------
def test_fake_text_provider_returns_n_variants() -> None:
    result = FakeTextProvider().generate(system="be nice", prompt="say hi", n=3)
    assert len(result.variants) == 3


def test_real_text_provider_sends_system_and_user_messages(
    text_transport: list[httpx.Request],
) -> None:
    result = LLMTextProvider().generate(system="be nice", prompt="say hi", n=2)

    sent = json.loads(text_transport[0].content)
    assert sent["n"] == 2
    assert sent["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "say hi"},
    ]
    assert result.variants[0].body == "a cosy morning, remastered"
    assert result.tokens_in == 12
    assert result.tokens_out == 34


def test_real_text_provider_carries_the_bearer_token(
    text_transport: list[httpx.Request], settings: Any
) -> None:
    settings.LLM_API_KEY = "sk-test-123"
    LLMTextProvider().generate(system="s", prompt="p", n=1)

    assert text_transport[0].headers["authorization"] == "Bearer sk-test-123"


def test_real_text_provider_classify_constraints_reports_reply_verbatim(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_completion("always show the handle"))

    _patch_transport(monkeypatch, handler)

    violated = LLMTextProvider().classify_constraints(
        image_bytes=b"png-bytes", restrictions=["always show the handle", "no hands in frame"]
    )

    assert violated == ["always show the handle"]


def test_classify_constraints_skips_the_call_with_no_restrictions() -> None:
    # No transport patched at all — a real call here would raise.
    assert LLMTextProvider().classify_constraints(image_bytes=b"x", restrictions=[]) == []


@override_settings(USE_FAKE_AI_PROVIDERS=True)
def test_fake_is_resolved_when_configured() -> None:
    assert isinstance(get_text_provider(), FakeTextProvider)


@override_settings(USE_FAKE_AI_PROVIDERS=False)
def test_real_is_resolved_otherwise() -> None:
    assert isinstance(get_text_provider(), LLMTextProvider)


@pytest.mark.integration
def test_live_llm_provider_completes_a_prompt() -> None:
    from django.conf import settings

    if not settings.LLM_API_KEY:
        pytest.skip("no LLM API key configured")

    result = LLMTextProvider().generate(system="Reply with one word.", prompt="Say hi.", n=1)
    assert result.variants


# -----------------------------------------------------------------------------
# ImageProvider
# -----------------------------------------------------------------------------
def test_fake_image_provider_returns_n_variants() -> None:
    result = FakeImageProvider().generate(
        prompt="a mug", reference_images=[], aspect="1:1", n=2, batch=False
    )
    assert len(result.variants) == 2


def test_real_image_provider_sync_hits_generate_content(
    image_transport: list[httpx.Request],
) -> None:
    result = NanoBananaImageProvider().generate(
        prompt="a mug on a table", reference_images=[], aspect="1:1", n=1, batch=False
    )

    assert image_transport[0].url.path.endswith(":generateContent")
    assert result.variants[0].mime == "image/png"


def test_real_image_provider_batch_submits_then_polls(image_transport: list[httpx.Request]) -> None:
    result = NanoBananaImageProvider().generate(
        prompt="a mug on a table", reference_images=[], aspect="1:1", n=1, batch=True
    )

    paths = [request.url.path for request in image_transport]
    assert any(p.endswith(":batchGenerateContent") for p in paths)
    assert any("/batches/1" in p for p in paths)
    assert result.variants


def test_real_image_provider_sends_reference_images_inline(
    image_transport: list[httpx.Request],
) -> None:
    NanoBananaImageProvider().generate(
        prompt="a mug", reference_images=[b"reference-bytes"], aspect="1:1", n=1, batch=False
    )

    sent = json.loads(image_transport[0].content)
    parts = sent["contents"][0]["parts"]
    assert parts[0] == {"text": "a mug"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"reference-bytes"


@override_settings(USE_FAKE_AI_PROVIDERS=True)
def test_fake_image_is_resolved_when_configured() -> None:
    assert isinstance(get_image_provider(), FakeImageProvider)


@override_settings(USE_FAKE_AI_PROVIDERS=False)
def test_real_image_is_resolved_otherwise() -> None:
    assert isinstance(get_image_provider(), NanoBananaImageProvider)


@pytest.mark.integration
def test_live_image_provider_generates_an_image() -> None:
    from django.conf import settings

    if not settings.IMAGE_PROVIDER_API_KEY:
        pytest.skip("no image provider API key configured")

    result = NanoBananaImageProvider().generate(
        prompt="a plain grey square", reference_images=[], aspect="1:1", n=1, batch=False
    )
    assert result.variants
