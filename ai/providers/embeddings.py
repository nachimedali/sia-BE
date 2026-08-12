"""Real `EmbeddingProvider`: the OpenAI-compatible `/embeddings` endpoint.

Same wire shape and same `LLM_BASE_URL` as `llm_text.py`, for the same reason
(design.md §9, A8): several vendors serve it, so targeting the shape rather than
one SDK keeps the swap a settings change.

Batched by construction — the endpoint takes a list — because the caller embeds
a whole ingest window at once and one request per item would turn clustering
into hundreds of round trips.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from ai.providers.base import unwrap_json_response
from common.exceptions import ProviderError


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.LLM_BASE_URL,
        headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


class OpenAIEmbeddingProvider:
    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        with _client() as client:
            payload = unwrap_json_response(
                client.post(
                    "/embeddings",
                    json={"model": model or settings.EMBEDDING_MODEL, "input": texts},
                ),
                label="Embedding provider",
            )

        data = payload.get("data", [])
        # The endpoint documents `index` rather than promising order, and the
        # caller pairs vectors back to items positionally — so sort rather than
        # trust, and refuse a short response instead of silently mis-pairing.
        vectors = [
            list(entry.get("embedding", []))
            for entry in sorted(data, key=lambda entry: int(entry.get("index", 0)))
        ]
        if len(vectors) != len(texts):
            raise ProviderError(
                "Embedding provider returned a different number of vectors than inputs.",
                detail={"requested": len(texts), "returned": len(vectors)},
            )
        return vectors


def get_embedding_provider() -> Any:
    """Resolves the configured provider. Swapping is a settings change (A8)."""
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        from ai.providers.fake import _fake_embedding_provider

        return _fake_embedding_provider
    return OpenAIEmbeddingProvider()
