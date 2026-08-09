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


class LLMTextProvider:
    def generate(
        self, *, system: str, prompt: str, n: int, model: str | None = None
    ) -> TextGenerationResult:
        model = model or settings.LLM_DEFAULT_MODEL
        started = time.monotonic()
        with _client() as client:
            payload = _post(
                client,
                {
                    "model": model,
                    "n": n,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
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

    def classify_constraints(self, *, image_bytes: bytes, restrictions: list[str]) -> list[str]:
        """Vision-as-labelling (design.md §5): one multimodal call, reused by
        the quality gate's brand-constraint check rather than a second port.
        """
        if not restrictions:
            return []

        encoded = base64.b64encode(image_bytes).decode()
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
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                                },
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
