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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from PIL import Image

from ai.providers.base import ImageGenerationResult, ImageVariant, unwrap_json_response
from ai.providers.switches import uses_fake
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


def _inline(part: dict[str, Any]) -> dict[str, Any] | None:
    """The image in a response part. The REST API answers in camelCase
    (`inlineData`/`mimeType`); snake_case is accepted too because requests may
    be written either way and some proxies echo the request's casing."""
    found = part.get("inlineData") or part.get("inline_data")
    return found if isinstance(found, dict) else None


def _variants_from_candidates(candidates: list[dict[str, Any]]) -> list[ImageVariant]:
    variants = []
    for candidate in candidates:
        for part in candidate.get("content", {}).get("parts", []):
            inline = _inline(part)
            if not inline or not inline.get("data"):
                continue
            content = base64.b64decode(inline["data"])
            with Image.open(io.BytesIO(content)) as img:
                width, height = img.size
            variants.append(
                ImageVariant(
                    content=content,
                    mime=inline.get("mimeType") or inline.get("mime_type") or "image/png",
                    width=width,
                    height=height,
                )
            )
    return variants


def _request(
    prompt: str, reference_images: list[bytes], aspect: str, size: str | None
) -> dict[str, Any]:
    """One image per request. Image models do not promise `candidateCount`
    above one, so `n` images are `n` requests — the same price, and a refusal
    of one does not lose the others."""
    image_config: dict[str, Any] = {"aspectRatio": aspect}
    if size:
        image_config["imageSize"] = size
    return {
        "contents": [{"parts": _parts(prompt, reference_images)}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": image_config,
        },
    }


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
        style: dict[str, float] | None = None,
        image_size: str | None = None,
    ) -> ImageGenerationResult:
        # `style` is the fake's colour-grade hint. This vendor is directed by
        # the prompt, which already carries every choice the grade came from,
        # so applying it here as well would grade the picture twice.
        model = model or settings.IMAGE_PROVIDER_MODEL
        started = time.monotonic()
        body = _request(prompt, reference_images, aspect, image_size)
        count = max(1, n)

        with _client() as client:
            if batch:
                responses = self._run_batch(client, model, [body] * count)
            else:
                responses = self._run_parallel(client, model, body, count)

        variants = [
            variant
            for payload in responses
            for variant in _variants_from_candidates(payload.get("candidates", []))
        ]
        if not variants:
            raise ProviderError("Image provider returned no candidates.")

        return ImageGenerationResult(
            variants=variants,
            provider=settings.IMAGE_PROVIDER,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _run_parallel(
        self, client: Any, model: str, body: dict[str, Any], count: int
    ) -> list[dict[str, Any]]:
        """`count` requests at once, on one client (httpx's is thread-safe):
        the wait is the slowest image, not the sum. A refused request costs
        its own image only; if every one is refused, the first refusal is
        raised so its status still reaches the routing fallback."""

        def one(_: int) -> dict[str, Any] | ProviderError:
            try:
                return self._unwrap(client.post(f"/models/{model}:generateContent", json=body))
            except ProviderError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=count) as pool:
            outcomes = list(pool.map(one, range(count)))
        replies = [outcome for outcome in outcomes if not isinstance(outcome, ProviderError)]
        if not replies:
            first = outcomes[0]
            assert isinstance(first, ProviderError)
            raise first
        return replies

    def _unwrap(self, response: Any) -> dict[str, Any]:
        return unwrap_json_response(response, label="Image provider")

    def _run_batch(
        self, client: Any, model: str, bodies: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Submits the requests inline as one batch job and polls it.

        **Bounded, and short for a batch** (`_BATCH_POLL_ATTEMPTS`): the vendor
        allows up to 24 hours. A job still running when the poll gives up is a
        `ProviderError`, so autopilot's batch route ships with `batch: false`
        (pricing.plan.md) until collection moves to a beat job.
        """
        submitted = self._unwrap(
            client.post(
                f"/models/{model}:batchGenerateContent",
                json={
                    "batch": {
                        "display_name": "composevision-images",
                        "input_config": {
                            "requests": {
                                "requests": [
                                    {"request": body, "metadata": {"key": str(index)}}
                                    for index, body in enumerate(bodies)
                                ]
                            }
                        },
                    }
                },
            )
        )
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
                output = status_payload.get("response", {})
                inlined = (output.get("inlinedResponses") or {}).get("inlinedResponses") or []
                return [item.get("response", {}) for item in inlined if isinstance(item, dict)]
            time.sleep(_BATCH_POLL_INTERVAL_SECONDS)

        raise ProviderError("Image batch job did not complete in time.", detail={"job": job_name})


def get_image_provider(task: str | None = None, *, workspace: Any = None) -> Any:
    """Resolves the configured provider. Swapping is a settings change (A8).

    With a `task`, bound to that task's route for the workspace's package —
    the model, the image size and the batch switch (`ai.services.routing`).
    """
    if uses_fake("IMAGE"):
        from ai.providers.fake import _fake_image_provider

        inner: Any = _fake_image_provider
    else:
        inner = NanoBananaImageProvider()
    if task is None:
        return inner
    from ai.providers.routed import RoutedImageProvider
    from ai.services.routing import resolve_for

    return RoutedImageProvider(inner, resolve_for(task, workspace))
