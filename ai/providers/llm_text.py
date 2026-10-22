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

import logging
import time
from typing import Any

from django.conf import settings
from redis.exceptions import RedisError

from ai.providers.base import (
    TextGenerationResult,
    TextVariant,
    image_url_part,
    unwrap_json_response,
)
from ai.providers.switches import uses_fake
from common.ratelimit import TokenBucket

logger = logging.getLogger(__name__)


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.LLM_BASE_URL,
        headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )


#: Swapped in tests; the adapter never sleeps in the suite (implementation.md §5).
_sleep = time.sleep


def _bucket() -> TokenBucket | None:
    per_minute = int(getattr(settings, "LLM_RATE_LIMIT_PER_MINUTE", 0) or 0)
    if per_minute <= 0:
        return None
    return TokenBucket(
        f"llm:{settings.LLM_PROVIDER}",
        capacity=per_minute,
        refill_per_second=per_minute / 60,
        namespace="ai",
    )


def _wait_for_slot() -> None:
    """Holds the call until the gateway's per-account budget has room.

    One bucket for every worker and every brand, because the vendor's limit is
    per account (NVIDIA's hosted API: about 40 requests a minute). Waiting is
    bounded; past `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` the call goes anyway and
    the vendor's own 429 handling below takes over. Redis being down never
    blocks a generation — it only loses the smoothing.
    """
    try:
        bucket = _bucket()
        if bucket is None:
            return
        waited = 0.0
        ceiling = float(settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS)
        while True:
            result = bucket.consume()
            if result.allowed or waited >= ceiling:
                return
            pause = min(max(result.retry_after, 0.1), ceiling - waited)
            _sleep(pause)
            waited += pause
    except RedisError:
        logger.warning("LLM rate bucket unavailable; calling without it", exc_info=True)


def _retry_after(response: Any) -> float:
    try:
        return float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return 2.0


def _post(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """One chat completion, inside the shared budget, retrying a 429 a bounded
    number of times. Any other failure is returned to the caller at once —
    a retry there is the routing layer's fallback, not this function's."""
    attempts = 1 + int(settings.LLM_RATE_LIMIT_RETRIES)
    for attempt in range(attempts):
        _wait_for_slot()
        response = client.post("/chat/completions", json=payload)
        if response.status_code != 429 or attempt == attempts - 1:
            return unwrap_json_response(response, label="LLM provider")
        _sleep(min(_retry_after(response), float(settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS)))
    raise AssertionError("unreachable")  # pragma: no cover


def _complete(*, messages: list[dict[str, Any]], model: str, n: int) -> TextGenerationResult:
    """Chat-completion calls, looped with n=1 until n variants are generated
    (ensuring compatibility with providers enforcing n=1). Parsed into the shape
    every text-returning method on this provider needs."""
    started = time.monotonic()
    variants: list[TextVariant] = []
    tokens_in = 0
    tokens_out = 0
    target_n = max(1, n)

    with _client() as client:
        while len(variants) < target_n:
            payload = _post(client, {"model": model, "n": 1, "messages": messages})
            choices = payload.get("choices", [])
            for choice in choices:
                body = choice.get("message", {}).get("content", "")
                variants.append(TextVariant(body=body))
            usage = payload.get("usage", {})
            tokens_in += usage.get("prompt_tokens", 0)
            tokens_out += usage.get("completion_tokens", 0)
            if not choices:
                break

    return TextGenerationResult(
        variants=variants,
        provider=settings.LLM_PROVIDER,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
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
                    "content": [{"type": "text", "text": prompt}, image_url_part(image_bytes)],
                },
            ],
            model=model or settings.LLM_DEFAULT_MODEL,
            n=n,
        )

    def classify_constraints(
        self, *, image_bytes: bytes, restrictions: list[str], model: str | None = None
    ) -> list[str]:
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
                    "model": model or settings.LLM_DEFAULT_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                image_url_part(image_bytes),
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


def get_text_provider(task: str | None = None, *, workspace: Any = None) -> Any:
    """Resolves the configured provider. Swapping is a settings change (A8).

    With a `task`, the provider comes bound to that task's route for the
    workspace's package (`ai.services.routing`): every production call site
    names its task, which `ai/tests/test_model_routing.py` enforces. Without
    one it is the bare adapter, for tests and the eval harness.
    """
    if uses_fake("TEXT"):
        from ai.providers.fake import _fake_text_provider

        inner: Any = _fake_text_provider
    else:
        inner = LLMTextProvider()
    if task is None:
        return inner
    from ai.providers.routed import RoutedTextProvider
    from ai.services.routing import resolve_for

    return RoutedTextProvider(inner, resolve_for(task, workspace))
