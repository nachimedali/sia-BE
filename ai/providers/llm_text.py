"""Real `TextProvider`: a generic OpenAI-Chat-Completions-compatible client.

design.md names "LLM provider" without pinning a vendor — several serve this
exact wire shape (OpenAI, and most OpenAI-compatible gateways), so targeting
it rather than one SDK keeps the vendor a config change (`LLM_BASE_URL`),
consistent with every other port in `ai/providers`.

`httpx`, called synchronously: every caller already runs inside a Celery task,
off the request/response cycle (design.md §11), so there is nothing here that
benefits from async.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from django.conf import settings

from ai.providers.base import TextGenerationResult, TextVariant, unwrap_json_response


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.LLM_BASE_URL,
        headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


def _post(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/chat/completions", json=payload)
    return unwrap_json_response(response, label="LLM provider")


def _image_url_part(image_bytes: bytes) -> dict[str, Any]:
    """One image, as a multimodal message part. The one place an image
    becomes a `data:` URL for this provider — `caption` and
    `classify_constraints` both attach a picture to a chat message and must
    agree on how."""
    encoded = base64.b64encode(image_bytes).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def _complete(*, messages: list[dict[str, Any]], model: str, n: int) -> TextGenerationResult:
    """One chat-completion call, parsed into the shape every text-returning
    method on this provider needs. `generate` and `caption` differ only in
    what messages they send — this is everything after that: the request,
    the variant list, the usage numbers, the timing."""
    started = time.monotonic()
    with _client() as client:
        payload = _post(client, {"model": model, "n": n, "messages": messages})
    variants = [
        TextVariant(body=choice["message"]["content"]) for choice in payload.get("choices", [])
    ]
    usage = payload.get("usage", {})
    return TextGenerationResult(
        variants=variants,
        provider=settings.LLM_PROVIDER,
        model=model,
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


class LLMTextProvider:
    def generate(
        self, *, system: str, prompt: str, n: int, model: str | None = None
    ) -> TextGenerationResult:
        return _complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            model=model or settings.LLM_DEFAULT_MODEL,
            n=n,
        )

    def caption(
        self, *, system: str, prompt: str, image_bytes: bytes, n: int, model: str | None = None
    ) -> TextGenerationResult:
        """The same chat endpoint as `generate`, with the image attached
        (P1-13) — the multimodal message shape `classify_constraints` below
        already uses, so there is one way this provider sends a picture.
        """
        return _complete(
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}, _image_url_part(image_bytes)],
                },
            ],
            model=model or settings.LLM_DEFAULT_MODEL,
            n=n,
        )

    def classify_constraints(self, *, image_bytes: bytes, restrictions: list[str]) -> list[str]:
        """Vision-as-labelling (design.md §5): one multimodal call, reused by
        the quality gate's brand-constraint check rather than a second port.

        Not routed through `_complete` — the response here is a classification
        read off raw text, not a `TextGenerationResult` of variants, so there
        is nothing of `_complete`'s parsing this call would reuse.
        """
        if not restrictions:
            return []

        instruction = (
            "List which of these constraints, if any, this image violates. "
            "Reply with one violated constraint per line, verbatim, or the "
            "single word NONE if it violates none.\n"
            + "\n".join(f"- {restriction}" for restriction in restrictions)
        )
        with _client() as client:
            payload = _post(
                client,
                {
                    "model": settings.LLM_DEFAULT_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                _image_url_part(image_bytes),
                            ],
                        }
                    ],
                },
            )
        content: str = payload["choices"][0]["message"]["content"]
        if content.strip().upper() == "NONE":
            return []
        reported = {line.strip("- ").strip() for line in content.splitlines() if line.strip()}
        return [restriction for restriction in restrictions if restriction in reported]


def get_text_provider() -> Any:
    """Resolves the configured provider. Swapping is a settings change (A8)."""
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        from ai.providers.fake import _fake_text_provider

        return _fake_text_provider
    return LLMTextProvider()
