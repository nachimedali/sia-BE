"""Real `ImageProvider`: Nano Banana 2 / Gemini 3.1 Flash Image (D8).

Batch vs sync (D8) is two endpoints on the same vendor API: `generateContent`
returns inline, `batchGenerateContent` returns a job the worker polls — bounded
polling is fine here because the Celery task *is* the async boundary
(design.md §11 already keeps this off the request/response cycle).

**Caveat, stated plainly:** this targets the publicly documented shape of
Gemini's `generateContent` family as of this writing. Nothing here has been
exercised against a live key (no credentials in this environment) — that is
exactly what `@pytest.mark.integration` is for (design.md §9). Verify the
field names against current vendor docs before the first real call in
production; this is real-adapter *param assembly*, tested against a stub in
`ai/tests/test_provider_contract.py`, not a guarantee the wire shape is
still current.
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any

from django.conf import settings
from PIL import Image

from ai.providers.base import ImageGenerationResult, ImageVariant, unwrap_json_response
from common.exceptions import ProviderError

_BATCH_POLL_ATTEMPTS = 30
_BATCH_POLL_INTERVAL_SECONDS = 2


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.IMAGE_PROVIDER_BASE_URL,
        params={"key": settings.IMAGE_PROVIDER_API_KEY},
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


def _parts(prompt: str, reference_images: list[bytes]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for image in reference_images:
        parts.append(
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image).decode()}}
        )
    return parts


def _variants_from_candidates(candidates: list[dict[str, Any]]) -> list[ImageVariant]:
    variants = []
    for candidate in candidates:
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inline_data")
            if not inline:
                continue
            content = base64.b64decode(inline["data"])
            with Image.open(io.BytesIO(content)) as img:
                width, height = img.size
            variants.append(
                ImageVariant(
                    content=content,
                    mime=inline.get("mime_type", "image/png"),
                    width=width,
                    height=height,
                )
            )
    return variants


class NanoBananaImageProvider:
    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        n: int,
        batch: bool,
        model: str | None = None,
    ) -> ImageGenerationResult:
        model = model or settings.IMAGE_PROVIDER_MODEL
        started = time.monotonic()
        body = {
            "contents": [{"parts": _parts(prompt, reference_images)}],
            "generationConfig": {"candidateCount": n, "aspectRatio": aspect},
        }

        with _client() as client:
            if batch:
                payload = self._run_batch(client, model, body)
            else:
                response = client.post(f"/models/{model}:generateContent", json=body)
                payload = self._unwrap(response)

        variants = _variants_from_candidates(payload.get("candidates", []))
        if not variants:
            raise ProviderError("Image provider returned no candidates.")

        return ImageGenerationResult(
            variants=variants,
            provider=settings.IMAGE_PROVIDER,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _unwrap(self, response: Any) -> dict[str, Any]:
        return unwrap_json_response(response, label="Image provider")

    def _run_batch(self, client: Any, model: str, body: dict[str, Any]) -> dict[str, Any]:
        submitted = self._unwrap(client.post(f"/models/{model}:batchGenerateContent", json=body))
        job_name = submitted.get("name")
        if not job_name:
            raise ProviderError("Image provider did not return a batch job id.")

        for _ in range(_BATCH_POLL_ATTEMPTS):
            status_payload = self._unwrap(client.get(f"/{job_name}"))
            if status_payload.get("done"):
                if "error" in status_payload:
                    raise ProviderError(
                        "Image batch job failed.", detail={"error": status_payload["error"]}
                    )
                response: dict[str, Any] = status_payload.get("response", {})
                return response
            time.sleep(_BATCH_POLL_INTERVAL_SECONDS)

        raise ProviderError("Image batch job did not complete in time.", detail={"job": job_name})


def get_image_provider() -> Any:
    """Resolves the configured provider. Swapping is a settings change (A8)."""
    if getattr(settings, "USE_FAKE_AI_PROVIDERS", False):
        from ai.providers.fake import _fake_image_provider

        return _fake_image_provider
    return NanoBananaImageProvider()
