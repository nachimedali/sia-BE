"""Real `VideoProvider`: OpenRouter's video API (pricing.audit.md §5).

One adapter for every video model OpenRouter lists — Grok Imagine, Wan, Kling,
Seedance, Hailuo… — chosen by slug. Which slug answers which task, for which
package, is an `AIModelRoute` row (`ai.services.routing`), so a model change is
an admin edit, never a deploy.

The contract, from OpenRouter's video-generation guide (read 2026-10-21):

* `POST /videos` with `model`, `prompt`, `duration`, `resolution`,
  `aspect_ratio`, `generate_audio` and, for image-to-video, `frame_images`
  (`{"type": "image_url", "image_url": {"url": …}, "frame_type":
  "first_frame" | "last_frame"}`) → **202** `{"id", "polling_url", "status"}`;
* `GET /videos/{id}` until `status` is `completed` (or `failed`, with `error`);
* `GET /videos/{id}/content?index=0` for the bytes, with the same key.

**Not yet observed against a live key.** Two points are the docs' word only
and are what the first live run must confirm (pricing.plan.md, Phase 9): that a
`data:` URL is accepted in `image_url.url` (the port hands this adapter bytes,
not a URL), and the `usage.cost` field this adapter logs per job.

Polling sleeps inside the worker. That is the same choice the image batch path
made and for the same reason: the Celery task on `media_q` *is* the async
boundary, and a render is minutes long either way.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

from django.conf import settings

from ai.providers.base import VideoResult, image_url_part, unwrap_json_response
from common.exceptions import ProviderError

logger = logging.getLogger(__name__)

#: Swapped in tests; the suite never sleeps (implementation.md §5).
_sleep = time.sleep

#: Ratios the routed models (Wan 3.0, Grok, Kling) refuse, sent as the nearest
#: one they take; the reel composer crops back to the row's own size.
_VENDOR_ASPECT = {"4:5": "3:4"}

_DONE = "completed"
_FAILED = {"failed", "cancelled", "expired"}


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.VIDEO_PROVIDER_BASE_URL,
        headers={"Authorization": f"Bearer {settings.VIDEO_PROVIDER_API_KEY}"},
        timeout=60,
    )


def mp4_duration_seconds(content: bytes) -> float | None:
    """The movie's length from its `mvhd` box, or `None` if there is none.

    Read rather than trusted: the quality gate compares the length a vendor
    *delivered* with the length the person paid for, and echoing back the
    requested duration would make that check pass on any file. A walk over the
    top-level boxes to `moov`, then its children to `mvhd` — no decoder.
    """

    def boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
        found = []
        offset = start
        while offset + 8 <= end:
            size, kind = struct.unpack(">I4s", data[offset : offset + 8])
            header = 8
            if size == 1 and offset + 16 <= end:
                size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
                header = 16
            elif size == 0:
                size = end - offset
            if size < header or offset + size > end:
                break
            found.append((kind, offset + header, offset + size))
            offset += size
        return found

    for kind, body, end in boxes(content, 0, len(content)):
        if kind != b"moov":
            continue
        for child, child_body, child_end in boxes(content, body, end):
            if child != b"mvhd" or child_end - child_body < 20:
                continue
            version = content[child_body]
            if version == 1 and child_end - child_body >= 32:
                timescale, duration = struct.unpack(
                    ">IQ", content[child_body + 20 : child_body + 32]
                )
            else:
                timescale, duration = struct.unpack(
                    ">II", content[child_body + 12 : child_body + 20]
                )
            return duration / timescale if timescale else None
    return None


class OpenRouterVideoProvider:
    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        duration_seconds: float,
        model: str | None = None,
        resolution: str | None = None,
        audio: bool | None = None,
    ) -> VideoResult:
        body = self._body(prompt, aspect, duration_seconds, model, resolution, audio)
        if reference_images:
            body["input_references"] = [image_url_part(image) for image in reference_images]
        return self._render(body, duration_seconds)

    def animate(
        self,
        *,
        image: bytes,
        role: str,
        prompt: str,
        aspect: str,
        duration_seconds: float,
        model: str | None = None,
        resolution: str | None = None,
        audio: bool | None = None,
    ) -> VideoResult:
        body = self._body(prompt, aspect, duration_seconds, model, resolution, audio)
        body["frame_images"] = [
            {
                **image_url_part(image),
                "frame_type": "last_frame" if role == "end" else "first_frame",
            }
        ]
        return self._render(body, duration_seconds)

    @staticmethod
    def _body(
        prompt: str,
        aspect: str,
        duration_seconds: float,
        model: str | None,
        resolution: str | None,
        audio: bool | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or settings.VIDEO_DEFAULT_MODEL,
            "prompt": prompt,
            # The vendors take whole seconds; the length rows are whole already.
            "duration": max(1, round(duration_seconds)),
            "aspect_ratio": _VENDOR_ASPECT.get(aspect, aspect),
        }
        if resolution:
            body["resolution"] = resolution
        if audio is not None:
            body["generate_audio"] = bool(audio)
        return body

    def _render(self, body: dict[str, Any], requested_seconds: float) -> VideoResult:
        started = time.monotonic()
        with _client() as client:
            submitted = unwrap_json_response(
                client.post("/videos", json=body), label="Video provider"
            )
            job_id = submitted.get("id")
            if not job_id:
                raise ProviderError("Video provider did not return a job id.")
            job = self._wait(client, str(job_id))
            content_response = client.get(f"/videos/{job_id}/content", params={"index": 0})
            if content_response.status_code >= 400:
                raise ProviderError(
                    f"Video provider returned {content_response.status_code} for the file.",
                    detail={"status": content_response.status_code, "job": job_id},
                )
            content = content_response.content

        usage = job.get("usage") or {}
        logger.info(
            "video job %s on %s finished: cost=%s byok=%s",
            job_id,
            body["model"],
            usage.get("cost"),
            usage.get("is_byok"),
        )
        measured = mp4_duration_seconds(content)
        return VideoResult(
            content=content,
            mime=content_response.headers.get("content-type", "video/mp4").split(";")[0],
            # Unreadable length → 0, which the quality gate refuses: an
            # unmeasured file is not charged as though it were the right length.
            duration_seconds=measured if measured is not None else 0.0,
            provider=settings.VIDEO_PROVIDER,
            model=str(body["model"]),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _wait(self, client: Any, job_id: str) -> dict[str, Any]:
        interval = max(1, int(settings.VIDEO_POLL_INTERVAL_SECONDS))
        deadline = float(settings.VIDEO_POLL_TIMEOUT_SECONDS)
        waited = 0.0
        while True:
            job = unwrap_json_response(client.get(f"/videos/{job_id}"), label="Video provider")
            status = str(job.get("status", ""))
            if status == _DONE:
                return job
            if status in _FAILED:
                raise ProviderError(
                    "Video provider did not finish the render.",
                    detail={"job": job_id, "status": status, "error": job.get("error")},
                )
            if waited >= deadline:
                raise ProviderError(
                    "Video provider did not finish in time.",
                    detail={"job": job_id, "status": status},
                )
            _sleep(interval)
            waited += interval
