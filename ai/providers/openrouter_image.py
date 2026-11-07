"""Second-vendor `ImageProvider`: OpenRouter's chat completions with image
output, used only when the primary image vendor (Google) fails.

Google stays the primary because it is cheaper (`pricing.audit.md` §4: no
5.5% fee, a batch tier). This exists for the day Google refuses outright — a
lapsed billing account answers 402 on every model, so the same-vendor route
fallback cannot help. Off until `IMAGE_FALLBACK_API_KEY` is set, deliberately
not read from `OPENROUTER_API_KEY` (the same rule as video).

Price is unchanged for the customer: credits come from `GenerationCost`, never
from which model answered (CLAUDE.md rule 19). `result.provider` and `.model`
name the vendor that actually drew, so `Generation.model` stays true.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from PIL import Image

from ai.providers.base import (
    ImageGenerationResult,
    ImageVariant,
    image_url_part,
    unwrap_json_response,
)
from common.exceptions import ProviderError

logger = logging.getLogger(__name__)

PROVIDER_KEY = "openrouter"


def fallback_enabled() -> bool:
    return bool(settings.IMAGE_FALLBACK_API_KEY)


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.IMAGE_FALLBACK_BASE_URL,
        headers={"Authorization": f"Bearer {settings.IMAGE_FALLBACK_API_KEY}"},
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


def _request(
    model: str, prompt: str, reference_images: list[bytes], aspect: str, size: str | None
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content += [image_url_part(image) for image in reference_images]
    image_config: dict[str, Any] = {"aspect_ratio": aspect}
    if size:
        image_config["image_size"] = size
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        # Image-only models (FLUX) refuse a request that also asks for text.
        "modalities": ["image"],
        "image_config": image_config,
    }


def _variants(payload: dict[str, Any]) -> list[ImageVariant]:
    variants = []
    for choice in payload.get("choices") or []:
        for image in (choice.get("message") or {}).get("images") or []:
            url = str((image.get("image_url") or {}).get("url") or "")
            if not url.startswith("data:") or ";base64," not in url:
                continue
            header, data = url.split(";base64,", 1)
            content = base64.b64decode(data)
            with Image.open(io.BytesIO(content)) as img:
                width, height = img.size
            variants.append(
                ImageVariant(
                    content=content,
                    mime=header.removeprefix("data:") or "image/png",
                    width=width,
                    height=height,
                )
            )
    return variants


class OpenRouterImageProvider:
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
        # `batch` and `style` are Google's and the fake's; OpenRouter has no
        # batch tier, so a batch request is drawn now, one request per image.
        model = model or settings.IMAGE_FALLBACK_MODEL
        started = time.monotonic()
        body = _request(model, prompt, reference_images, aspect, image_size)
        count = max(1, n)

        def one(_: int) -> dict[str, Any] | ProviderError:
            try:
                return unwrap_json_response(
                    client.post("/chat/completions", json=body), label="Fallback image provider"
                )
            except ProviderError as exc:
                return exc

        with _client() as client, ThreadPoolExecutor(max_workers=count) as pool:
            outcomes = list(pool.map(one, range(count)))
        replies = [outcome for outcome in outcomes if not isinstance(outcome, ProviderError)]
        if not replies:
            first = outcomes[0]
            assert isinstance(first, ProviderError)
            raise first

        variants = [variant for payload in replies for variant in _variants(payload)]
        if not variants:
            raise ProviderError("Fallback image provider returned no image.")
        return ImageGenerationResult(
            variants=variants,
            provider=PROVIDER_KEY,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class ImageWithVendorFallback:
    """The primary port (with its own route and same-vendor fallback), then
    once on the second vendor — for any failure, a 402 or a 429 included,
    because the second vendor is a different account with its own limits."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary = primary
        self.fallback = fallback

    def generate(self, **kwargs: Any) -> Any:
        try:
            return self.primary.generate(**kwargs)
        except ProviderError:
            logger.warning(
                "image: primary vendor failed, trying %s/%s",
                PROVIDER_KEY,
                settings.IMAGE_FALLBACK_MODEL,
                exc_info=True,
            )
            # The primary's model names mean nothing to the second vendor.
            return self.fallback.generate(**{**kwargs, "model": None})
