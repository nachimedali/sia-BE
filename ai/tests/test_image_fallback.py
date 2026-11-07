"""The second image vendor (OpenRouter, FLUX 3 Klein) answers when Google fails."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from django.test import override_settings

from ai.providers.nanobanana_image import NanoBananaImageProvider, get_image_provider
from ai.providers.openrouter_image import ImageWithVendorFallback, OpenRouterImageProvider
from common.exceptions import ProviderError
from tests.httpx_transport import patch_httpx_transport

PNG_1X1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100"
        "00000000000000000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
).decode()

OPENROUTER_REPLY = {
    "choices": [
        {
            "message": {
                "images": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1X1}"}}
                ]
            }
        }
    ]
}


def _transport(monkeypatch: Any, *, google: int, openrouter: int) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.host == "openrouter.ai":
            if openrouter != 200:
                return httpx.Response(openrouter, json={"error": "no"})
            return httpx.Response(200, json=OPENROUTER_REPLY)
        return httpx.Response(google, json={"error": {"message": "billing"}})

    patch_httpx_transport(monkeypatch, handler)
    return captured


def _draw(provider: Any) -> Any:
    return provider.generate(
        prompt="a mug", reference_images=[b"ref"], aspect="9:16", n=1, batch=False
    )


FALLBACK_ON = {
    "USE_FAKE_AI_PROVIDERS": False,
    "USE_FAKE_IMAGE_PROVIDER": False,
    "IMAGE_FALLBACK_API_KEY": "or-key",
}


def test_the_request_is_openrouter_image_output_with_the_reference(monkeypatch: Any) -> None:
    sent = _transport(monkeypatch, google=402, openrouter=200)
    result = _draw(OpenRouterImageProvider())

    request = sent[0]
    body = json.loads(request.content)
    assert request.url.path.endswith("/chat/completions")
    assert body["model"] == "black-forest-labs/flux-3-image-klein"
    assert body["modalities"] == ["image"]
    assert body["image_config"] == {"aspect_ratio": "9:16"}
    assert body["messages"][0]["content"][1]["type"] == "image_url"
    assert (result.provider, result.model) == ("openrouter", "black-forest-labs/flux-3-image-klein")
    assert result.variants[0].mime == "image/png"


@override_settings(**FALLBACK_ON)
def test_google_refusing_hands_the_draw_to_flux(monkeypatch: Any) -> None:
    sent = _transport(monkeypatch, google=402, openrouter=200)
    provider = get_image_provider()
    assert isinstance(provider, ImageWithVendorFallback)

    result = _draw(provider)
    assert result.provider == "openrouter"
    assert [r.url.host for r in sent] == ["generativelanguage.googleapis.com", "openrouter.ai"]
    assert sent[1].headers["authorization"] == "Bearer or-key"


@override_settings(**FALLBACK_ON)
def test_both_vendors_failing_is_still_a_provider_error(monkeypatch: Any) -> None:
    _transport(monkeypatch, google=500, openrouter=503)
    with pytest.raises(ProviderError):
        _draw(get_image_provider())


@override_settings(USE_FAKE_AI_PROVIDERS=False, USE_FAKE_IMAGE_PROVIDER=False)
def test_no_fallback_key_means_google_alone() -> None:
    assert isinstance(get_image_provider(), NanoBananaImageProvider)
