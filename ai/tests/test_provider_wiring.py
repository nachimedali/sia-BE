"""The real adapters behind the routes, against a mocked transport (no network).

- OpenRouter video (`ai.providers.openrouter_video`): what is submitted for a
  first and a last frame, the poll until `completed`, the download, a length
  read from the file rather than echoed, and the failures — a failed job, a
  job that never finishes, a missing file.
- The NVIDIA text adapter's shared request budget and its 429 retry.
- The Google image adapter: `imageConfig.imageSize`, one request per image,
  either response casing, and the inline batch shape.

The live runs are marked `integration` and skipped without a key. They spend
money and are run by hand in Phase 9 of pricing.plan.md, never in the suite.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

import httpx
import pytest
from django.test import override_settings

from ai.providers import llm_text, openrouter_video
from ai.providers.llm_text import LLMTextProvider
from ai.providers.nanobanana_image import NanoBananaImageProvider
from ai.providers.openrouter_video import OpenRouterVideoProvider, mp4_duration_seconds
from ai.providers.video import get_video_provider
from common.exceptions import ProviderError
from tests.httpx_transport import patch_httpx_transport

VIDEO_KEY = {"VIDEO_PROVIDER_API_KEY": "or-test-key", "USE_FAKE_AI_PROVIDERS": False}


def _mp4(seconds: float, *, timescale: int = 1000) -> bytes:
    """A minimal ISO-BMFF file: `ftyp`, then `moov` holding a version-0 `mvhd`."""
    ftyp = struct.pack(">I4s", 20, b"ftyp") + b"isom" + b"\x00\x00\x02\x00" + b"isom"
    mvhd_body = (
        b"\x00\x00\x00\x00"  # version 0, flags
        + struct.pack(">II", 0, 0)  # creation, modification
        + struct.pack(">II", timescale, int(seconds * timescale))
        + b"\x00" * 80
    )
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    return ftyp + moov


@pytest.fixture
def no_sleep(monkeypatch: Any) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(openrouter_video, "_sleep", slept.append)
    monkeypatch.setattr(llm_text, "_sleep", slept.append)
    return slept


def _video_transport(
    monkeypatch: Any,
    *,
    statuses: list[str],
    content: bytes | None = None,
    content_status: int = 200,
) -> list[httpx.Request]:
    captured: list[httpx.Request] = []
    remaining = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/videos"):
            return httpx.Response(
                202, json={"id": "job_1", "polling_url": "/videos/job_1", "status": "pending"}
            )
        if path.endswith("/videos/job_1/content"):
            if content_status >= 400:
                return httpx.Response(content_status, text="gone")
            return httpx.Response(
                200, content=content or _mp4(5), headers={"content-type": "video/mp4"}
            )
        if path.endswith("/videos/job_1"):
            status = remaining.pop(0) if remaining else "in_progress"
            body: dict[str, Any] = {"id": "job_1", "status": status}
            if status == "completed":
                body["usage"] = {"cost": 0.25, "is_byok": False}
                body["unsigned_urls"] = ["/api/v1/videos/job_1/content?index=0"]
            if status == "failed":
                body["error"] = "content policy"
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    patch_httpx_transport(monkeypatch, handler)
    return captured


# -----------------------------------------------------------------------------
# OpenRouter video
# -----------------------------------------------------------------------------
@override_settings(**VIDEO_KEY)
def test_a_first_frame_clip_is_submitted_polled_and_downloaded(monkeypatch, no_sleep) -> None:
    captured = _video_transport(monkeypatch, statuses=["pending", "in_progress", "completed"])

    result = OpenRouterVideoProvider().animate(
        image=b"\x89PNG still",
        role="start",
        prompt="slow push-in",
        aspect="9:16",
        duration_seconds=5,
        model="x-ai/grok-imagine-video-1.5-lite",
        resolution="720p",
    )

    submitted = json.loads(captured[0].content)
    assert captured[0].headers["Authorization"] == "Bearer or-test-key"
    assert submitted["model"] == "x-ai/grok-imagine-video-1.5-lite"
    assert (submitted["duration"], submitted["aspect_ratio"], submitted["resolution"]) == (
        5,
        "9:16",
        "720p",
    )
    assert "generate_audio" not in submitted  # no route said, so the vendor decides
    frame = submitted["frame_images"][0]
    assert frame["frame_type"] == "first_frame"
    assert frame["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(frame["image_url"]["url"].split(",", 1)[1]) == b"\x89PNG still"
    assert [r.url.path for r in captured].count("/api/v1/videos/job_1") == 3
    assert captured[-1].url.params["index"] == "0"
    assert no_sleep == [10, 10]
    assert result.mime == "video/mp4"
    assert result.duration_seconds == pytest.approx(5.0)
    assert (result.provider, result.model) == ("openrouter", "x-ai/grok-imagine-video-1.5-lite")


@override_settings(**VIDEO_KEY)
def test_a_feed_clip_is_asked_for_at_3_4_which_the_vendors_take(monkeypatch, no_sleep) -> None:
    captured = _video_transport(monkeypatch, statuses=["completed"])

    OpenRouterVideoProvider().animate(
        image=b"\x89PNG still", role="start", prompt="p", aspect="4:5", duration_seconds=5
    )

    assert json.loads(captured[0].content)["aspect_ratio"] == "3:4"


@override_settings(**VIDEO_KEY)
def test_a_last_frame_clip_asks_for_the_last_frame_and_audio(monkeypatch, no_sleep) -> None:
    captured = _video_transport(monkeypatch, statuses=["completed"])

    OpenRouterVideoProvider().animate(
        image=b"\xff\xd8jpeg",
        role="end",
        prompt="settle",
        aspect="1:1",
        duration_seconds=10,
        model="alibaba/wan-3.0",
        resolution="1080p",
        audio=True,
    )

    submitted = json.loads(captured[0].content)
    assert submitted["frame_images"][0]["frame_type"] == "last_frame"
    assert submitted["frame_images"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert submitted["generate_audio"] is True


@override_settings(**VIDEO_KEY)
def test_without_a_model_the_settings_default_is_sent(monkeypatch, no_sleep) -> None:
    captured = _video_transport(monkeypatch, statuses=["completed"])

    OpenRouterVideoProvider().generate(
        prompt="a mug", reference_images=[b"ref"], aspect="16:9", duration_seconds=5
    )

    submitted = json.loads(captured[0].content)
    assert submitted["model"] == "x-ai/grok-imagine-video-1.5-lite"
    assert submitted["input_references"][0]["type"] == "image_url"
    assert "frame_images" not in submitted


@override_settings(**VIDEO_KEY)
def test_the_length_is_read_from_the_file_not_echoed(monkeypatch, no_sleep) -> None:
    """The quality gate compares the delivered length with the paid one."""
    _video_transport(monkeypatch, statuses=["completed"], content=_mp4(2.0))

    result = OpenRouterVideoProvider().animate(
        image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
    )

    assert result.duration_seconds == pytest.approx(2.0)


@override_settings(**VIDEO_KEY)
def test_an_unmeasurable_file_reports_zero_length(monkeypatch, no_sleep) -> None:
    _video_transport(monkeypatch, statuses=["completed"], content=b"not an mp4 at all")

    result = OpenRouterVideoProvider().animate(
        image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
    )

    assert result.duration_seconds == 0.0


@override_settings(**VIDEO_KEY)
def test_a_failed_job_is_a_provider_error_carrying_the_reason(monkeypatch, no_sleep) -> None:
    _video_transport(monkeypatch, statuses=["in_progress", "failed"])

    with pytest.raises(ProviderError) as caught:
        OpenRouterVideoProvider().animate(
            image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
        )
    assert caught.value.payload["status"] == "failed"
    assert caught.value.payload["error"] == "content policy"


@override_settings(**VIDEO_KEY, VIDEO_POLL_TIMEOUT_SECONDS=30, VIDEO_POLL_INTERVAL_SECONDS=10)
def test_a_job_that_never_finishes_gives_up_at_the_timeout(monkeypatch, no_sleep) -> None:
    captured = _video_transport(monkeypatch, statuses=[])

    with pytest.raises(ProviderError, match="in time"):
        OpenRouterVideoProvider().animate(
            image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
        )
    assert no_sleep == [10, 10, 10]
    assert not any(r.url.path.endswith("/content") for r in captured)


@override_settings(**VIDEO_KEY)
def test_a_missing_file_is_a_provider_error(monkeypatch, no_sleep) -> None:
    _video_transport(monkeypatch, statuses=["completed"], content_status=404)

    with pytest.raises(ProviderError, match="for the file"):
        OpenRouterVideoProvider().animate(
            image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
        )


@override_settings(**VIDEO_KEY)
def test_a_refused_submission_carries_the_vendors_status(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "duration must be one of 4, 6, 8"})

    patch_httpx_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        OpenRouterVideoProvider().animate(
            image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
        )
    assert caught.value.payload["status"] == 400


def test_the_mp4_reader_handles_version_one_and_large_boxes() -> None:
    body = (
        b"\x01\x00\x00\x00"
        + struct.pack(">QQ", 0, 0)
        + struct.pack(">IQ", 600, 6000)
        + b"\x00" * 80
    )
    mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
    moov = struct.pack(">I4sQ", 1, b"moov", 16 + len(mvhd)) + mvhd
    assert mp4_duration_seconds(moov) == pytest.approx(10.0)
    assert mp4_duration_seconds(b"") is None


@override_settings(USE_FAKE_AI_PROVIDERS=False, VIDEO_PROVIDER_API_KEY="")
def test_without_a_key_there_is_no_video_provider() -> None:
    assert get_video_provider() is None


@override_settings(**VIDEO_KEY)
def test_with_a_key_the_openrouter_adapter_answers() -> None:
    assert isinstance(get_video_provider(), OpenRouterVideoProvider)


@override_settings(**VIDEO_KEY, VIDEO_PROVIDER="somewhere-else")
def test_an_unknown_video_provider_is_a_deployment_error() -> None:
    with pytest.raises(NotImplementedError):
        get_video_provider()


@override_settings(
    USE_FAKE_AI_PROVIDERS=False, OPENROUTER_API_KEY="or-key", VIDEO_PROVIDER_API_KEY=""
)
def test_an_openrouter_key_alone_never_makes_video_real() -> None:
    """Setting `OPENROUTER_API_KEY` must not make a dev stack spend."""
    assert get_video_provider() is None


@pytest.mark.integration
def test_live_video_render() -> None:  # pragma: no cover - run by hand, spends money
    from django.conf import settings

    if not settings.VIDEO_PROVIDER_API_KEY:
        pytest.skip("no video provider key configured")
    result = OpenRouterVideoProvider().generate(
        prompt="a plain grey square, still",
        reference_images=[],
        aspect="1:1",
        duration_seconds=3,
        resolution="480p",
    )
    assert result.content


# -----------------------------------------------------------------------------
# NVIDIA text: the shared budget and the 429 retry
# -----------------------------------------------------------------------------
def _chat(content: str = "ok") -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}], "usage": {}}


@override_settings(LLM_RATE_LIMIT_RETRIES=2, LLM_RATE_LIMIT_MAX_WAIT_SECONDS=30)
def test_a_429_is_retried_after_the_vendors_retry_after(monkeypatch, no_sleep) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
        return httpx.Response(200, json=_chat("recovered"))

    patch_httpx_transport(monkeypatch, handler)
    result = LLMTextProvider().generate(system="s", prompt="p", n=1, model="m")

    assert result.variants[0].body == "recovered"
    assert len(calls) == 2
    assert no_sleep == [3.0]


@override_settings(LLM_RATE_LIMIT_RETRIES=1, LLM_RATE_LIMIT_MAX_WAIT_SECONDS=30)
def test_a_429_that_persists_surfaces_as_a_rate_limit(monkeypatch, no_sleep) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    patch_httpx_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        LLMTextProvider().generate(system="s", prompt="p", n=1, model="m")
    assert caught.value.payload["status"] == 429


@override_settings(LLM_RATE_LIMIT_RETRIES=2)
def test_any_other_failure_is_not_retried_here(monkeypatch, no_sleep) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="model not found")

    patch_httpx_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        LLMTextProvider().generate(system="s", prompt="p", n=1, model="m")
    assert caught.value.payload["status"] == 404
    assert len(calls) == 1


@override_settings(LLM_RATE_LIMIT_PER_MINUTE=2, LLM_RATE_LIMIT_MAX_WAIT_SECONDS=30)
def test_the_shared_bucket_makes_the_third_call_wait(monkeypatch) -> None:
    from common.redis import get_redis

    get_redis().delete("ai:llm:nvidia")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat())

    patch_httpx_transport(monkeypatch, handler)
    # A clock that only moves when the adapter sleeps, so the wait is exact.
    clock = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("common.ratelimit.time.time", lambda: clock[0])
    monkeypatch.setattr(llm_text, "_sleep", sleep)

    for _ in range(3):
        LLMTextProvider().generate(system="s", prompt="p", n=1, model="m")

    # Two tokens at the start; the third waits for one to refill (30 s at 2/min).
    assert slept == [pytest.approx(30.0)]
    get_redis().delete("ai:llm:nvidia")


@override_settings(LLM_RATE_LIMIT_PER_MINUTE=40)
def test_redis_being_down_never_blocks_a_call(monkeypatch, no_sleep) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    class _Down:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RedisConnectionError("down")

    monkeypatch.setattr(llm_text, "TokenBucket", _Down)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat("through"))

    patch_httpx_transport(monkeypatch, handler)
    assert LLMTextProvider().generate(system="s", prompt="p", n=1).variants[0].body == "through"


def test_the_constraint_check_sends_the_routed_model(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_chat("NONE"))

    patch_httpx_transport(monkeypatch, handler)
    LLMTextProvider().classify_constraints(
        image_bytes=b"\x89PNG", restrictions=["no alcohol"], model="vision-model"
    )

    assert sent[0]["model"] == "vision-model"


# -----------------------------------------------------------------------------
# Google images
# -----------------------------------------------------------------------------
_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100"
        "00000000000000000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
).decode()


def _image_reply(*, camel: bool = True) -> dict[str, Any]:
    part = (
        {"inlineData": {"mimeType": "image/png", "data": _PNG}}
        if camel
        else {"inline_data": {"mime_type": "image/png", "data": _PNG}}
    )
    return {"candidates": [{"content": {"parts": [{"text": "here you go"}, part]}}]}


def test_the_route_size_reaches_image_config_one_request_per_image(monkeypatch) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=_image_reply())

    patch_httpx_transport(monkeypatch, handler)
    result = NanoBananaImageProvider().generate(
        prompt="a mug",
        reference_images=[],
        aspect="4:5",
        n=3,
        batch=False,
        model="gemini-nano-banana-2.1",
        image_size="2K",
    )

    assert len(sent) == 3 and len(result.variants) == 3
    body = json.loads(sent[0].content)
    assert sent[0].url.path.endswith("/models/gemini-nano-banana-2.1:generateContent")
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "4:5", "imageSize": "2K"}
    assert "candidateCount" not in body["generationConfig"]
    assert result.model == "gemini-nano-banana-2.1"


def test_without_a_size_none_is_sent(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_image_reply(camel=False))

    patch_httpx_transport(monkeypatch, handler)
    result = NanoBananaImageProvider().generate(
        prompt="a mug", reference_images=[], aspect="1:1", n=1, batch=False
    )

    assert sent[0]["generationConfig"]["imageConfig"] == {"aspectRatio": "1:1"}
    assert result.variants[0].mime == "image/png"


def test_the_batch_submits_every_image_inline_in_one_job(monkeypatch) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith(":batchGenerateContent"):
            return httpx.Response(200, json={"name": "batches/7"})
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": {
                    "inlinedResponses": {
                        "inlinedResponses": [
                            {"response": _image_reply()},
                            {"response": _image_reply()},
                        ]
                    }
                },
            },
        )

    patch_httpx_transport(monkeypatch, handler)
    result = NanoBananaImageProvider().generate(
        prompt="a mug", reference_images=[], aspect="1:1", n=2, batch=True, image_size="1K"
    )

    job = json.loads(sent[0].content)["batch"]
    requests = job["input_config"]["requests"]["requests"]
    assert [r["metadata"]["key"] for r in requests] == ["0", "1"]
    assert requests[0]["request"]["generationConfig"]["imageConfig"]["imageSize"] == "1K"
    assert len(result.variants) == 2


def test_one_refused_image_costs_only_that_image(monkeypatch) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="busy")
        return httpx.Response(200, json=_image_reply())

    patch_httpx_transport(monkeypatch, handler)
    result = NanoBananaImageProvider().generate(
        prompt="a mug", reference_images=[], aspect="1:1", n=3, batch=False
    )

    assert len(calls) == 3
    assert len(result.variants) == 2


def test_every_image_refused_raises_the_vendors_status(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    patch_httpx_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        NanoBananaImageProvider().generate(
            prompt="a mug", reference_images=[], aspect="1:1", n=2, batch=False
        )
    assert caught.value.payload["status"] == 429


def test_a_timeout_is_a_provider_error_so_the_route_can_fall_back(monkeypatch, no_sleep) -> None:
    """A slow model is a failed model: the routing layer's fallback only runs
    on a `ProviderError`, so a raw `httpx` timeout used to skip it entirely."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    patch_httpx_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as caught:
        LLMTextProvider().generate(system="s", prompt="p", n=1, model="m")
    assert caught.value.payload["reason"] == "ReadTimeout"


@pytest.mark.django_db
@override_settings(
    USE_FAKE_AI_PROVIDERS=False,
    USE_FAKE_TEXT_PROVIDER=False,
    LLM_TIMEOUT_SECONDS=30,
    LLM_TASK_TIMEOUT_SECONDS={"brand_import": 120},
)
def test_a_background_task_can_wait_longer_than_the_default(monkeypatch) -> None:
    """The website import runs in a worker and reads ten pages, so a reasoning
    model needs more than the 30 s an in-request call is held to."""
    from ai.models import AITask
    from ai.providers.llm_text import get_text_provider

    waited: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        waited.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_chat())

    patch_httpx_transport(monkeypatch, handler)
    get_text_provider(AITask.BRAND_IMPORT).generate(system="s", prompt="p", n=1)
    get_text_provider(AITask.TOOLS).generate(system="s", prompt="p", n=1)

    assert waited == [120, 30]
