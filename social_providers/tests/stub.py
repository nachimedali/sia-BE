"""A stateful stand-in for SocialAPI's post and media endpoints (SA-31…33).

Behaves the way the vendor documents (`docs.social-api.ai`, fetched
2026-10-13), including the parts exactly-once depends on:

* `POST /posts` without `publish_now` stores a **draft**; nothing goes live.
* `POST /posts/{id}/publish` works **once**: a second call is
  `409 post.state_invalid`. The post is then `publishing` until `settle`.
* `DELETE /posts/{id}` on a live post would delete it from the platforms; the
  stub records that in `deleted_live` so a test can assert it never happens.
* Media: server-side upload, the presigned path (upload-url → PUT → verify),
  and `409 media.in_use` on delete.

Knobs a test sets to make the vendor misbehave: `validate_errors`,
`validate_warnings`, `drop_media` (stores the post without its media),
`publish_failure` (an error envelope for the publish call), `fast_publish`
(the publish answers already published).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from social_providers.socialapi.budget import TeamBudget
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.publish import SocialAPIAdapter

KEY = "sapi_key_" + "c" * 62
STORAGE = "storage.stub.test"


@dataclass
class MemoryJournal:
    """A `PublishJournal` held in memory, for driving an adapter directly."""

    provider_post_id: str = ""
    provider_state: str = ""
    provider_media_ids: list[str] = field(default_factory=list)

    def record(
        self,
        *,
        provider_post_id: str | None = None,
        provider_state: str | None = None,
        media_ids: list[str] | None = None,
    ) -> None:
        if provider_post_id is not None:
            self.provider_post_id = provider_post_id
        if provider_state is not None:
            self.provider_state = provider_state
        if media_ids is not None:
            self.provider_media_ids = list(media_ids)


def error(status: int, code: str, message: str = "refused") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


class StubVendor:
    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.media: dict[str, str] = {}  # id → "ready" | "pending"
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.deleted_live: list[str] = []
        self.deleted_media: list[str] = []
        self.media_in_use: set[str] = set()
        self.validate_errors: list[dict[str, Any]] = []
        self.validate_warnings: list[dict[str, Any]] = []
        self.drop_media = False
        self.fast_publish = False
        self.publish_failure: httpx.Response | None = None
        self.unreachable_on: str = ""
        self._n = 0

    # --- what a test reads ----------------------------------------------------------
    def count(self, method: str, path_prefix: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p.startswith(path_prefix))

    @property
    def creates(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p == "/posts")

    @property
    def publishes(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/publish"))

    def settle(self, pid: str, *, published: bool = True, platform_post_id: str = "") -> None:
        post = self.posts[pid]
        target = post["targets"][0]
        if published:
            post["status"] = "published"
            target.update(
                status="published",
                platform_post_id=platform_post_id or f"native-{pid}",
                permalink=f"https://platform.test/p/{pid}",
                published_at="2026-10-13T10:00:00Z",
            )
        else:
            post["status"] = "failed"
            target.update(
                status="failed",
                error={"code": "platform.rejected", "message": "Video too long for this account."},
            )

    def adapter(self) -> SocialAPIAdapter:
        client = SocialAPIClient(
            api_key=KEY,
            base_url="https://api.social-api.ai/v1",
            transport=httpx.MockTransport(self.handle),
            budget=TeamBudget(),
            sleep=lambda _s: None,
            sample=lambda: 1.0,
        )
        return SocialAPIAdapter(client)

    # --- the vendor -----------------------------------------------------------------
    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == STORAGE:
            self.calls.append((request.method, "storage"))
            return httpx.Response(200)
        path = request.url.path.removeprefix("/v1")
        if self.unreachable_on and f"{request.method} {path}".startswith(self.unreachable_on):
            # Lost on the wire: the vendor never sees it.
            self.unreachable_on = ""
            raise httpx.ConnectError("vendor unreachable")
        self.calls.append((request.method, path))
        payload: dict[str, Any] = {}
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            payload = json.loads(request.content)
            self.bodies.append(payload)
        parts = [p for p in path.split("/") if p]

        if path == "/posts/validate" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "valid": not self.validate_errors,
                    "errors": self.validate_errors,
                    "warnings": self.validate_warnings,
                },
            )
        if path == "/posts" and request.method == "POST":
            return self._create(payload)
        if path == "/posts" and request.method == "GET":
            rows = [
                p for p in self.posts.values() if p["status"] == request.url.params.get("status")
            ]
            return httpx.Response(200, json={"data": rows})
        if parts[:1] == ["posts"] and len(parts) == 2:
            return self._post(parts[1], request.method)
        if parts[:1] == ["posts"] and parts[2:] == ["publish"]:
            return self._publish(parts[1])
        if path == "/media/upload":
            media_id = self._id("media")
            self.media[media_id] = "ready"
            return httpx.Response(201, json={"media_id": media_id})
        if path == "/media/upload-url":
            media_id = self._id("media")
            self.media[media_id] = "pending"
            return httpx.Response(
                200, json={"media_id": media_id, "upload_url": f"https://{STORAGE}/{media_id}"}
            )
        if parts[:1] == ["media"] and parts[2:] == ["verify"]:
            self.media[parts[1]] = "ready"
            return httpx.Response(200, json={"id": parts[1], "status": "ready"})
        if parts[:1] == ["media"] and len(parts) == 2 and request.method == "DELETE":
            if parts[1] in self.media_in_use:
                return error(409, "media.in_use")
            if parts[1] not in self.media:
                return error(404, "resource.not_found")
            self.media.pop(parts[1])
            self.deleted_media.append(parts[1])
            return httpx.Response(204)
        return error(404, "resource.not_found", "no route")

    def _create(self, payload: dict[str, Any]) -> httpx.Response:
        if payload.get("publish_now") or payload.get("scheduled_at"):
            raise AssertionError("we never ask the vendor to publish or schedule on create")
        pid = self._id("post")
        targets = [
            {"account_id": t["account_id"], "status": "pending", "platform_post_id": ""}
            for t in payload.get("targets") or []
        ]
        self.posts[pid] = {
            "id": pid,
            "status": "draft",
            "text": payload.get("text", ""),
            "media": [] if self.drop_media else list(payload.get("media") or []),
            "targets": targets,
            "created_at": (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat(),
        }
        return httpx.Response(201, json=self.posts[pid])

    def _post(self, pid: str, method: str) -> httpx.Response:
        post = self.posts.get(pid)
        if post is None:
            return error(404, "resource.not_found")
        if method == "GET":
            return httpx.Response(200, json=post)
        if method == "DELETE":
            if post["status"] in ("publishing", "published", "partial"):
                self.deleted_live.append(pid)
            self.posts.pop(pid)
            return httpx.Response(200, json={"success": True, "deleted": True})
        return error(405, "method")

    def _publish(self, pid: str) -> httpx.Response:
        post = self.posts.get(pid)
        if post is None:
            return error(404, "resource.not_found")
        if self.publish_failure is not None:
            answer, self.publish_failure = self.publish_failure, None
            if answer.status_code == 422:
                post["status"] = "failed"
            return answer
        if post["status"] not in ("draft", "scheduled"):
            return error(409, "post.state_invalid", "Post is not in a publishable state")
        post["status"] = "publishing"
        post["targets"][0]["status"] = "publishing"
        if self.fast_publish:
            self.settle(pid)
        return httpx.Response(201, json=post)
