"""Publishing through SocialAPI, exactly once (SA-31, SA-32).

SocialAPI has **no idempotency key on `POST /posts`** (plan G1), and its
publish is **asynchronous** (`publishing` → `published | failed`, G9). Both are
answered with one journal, written before each step is taken:

```
media      upload (dev/private storage) or a fresh signed URL   journal: media ids
validate   POST /posts/validate — errors, or a warning that names a setting
           we sent (the vendor would drop it), refuse before anything exists
draft      POST /posts (no publish_now): free, never live       journal: draft_created + id
read back  the stored draft still has our text, media and account, or it is
           deleted and the publish refused
publish    POST /posts/{id}/publish                             journal: publish_requested
outcome    from the answer, or later from GET /posts/{id} (the reconciler)
```

A retry reads the journal first and asks the vendor where the post stands, so
the only thing a crash can leave behind is an **unpublished draft** — swept by
the reconciler — never a second live post. A `409 post.state_invalid` on
publish means "already requested" and is read back, not retried. A post still
`publishing` is never published again: TikTok in particular confirms an upload
minutes before it shows, and a second request posts twice.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
from collections.abc import Iterator, Sequence
from typing import IO, Any
from urllib.parse import urlparse

from django.conf import settings

from channels.adapters.base import PlatformError, PublishJournal, PublishMedia, PublishResult
from common.timestamps import parse_or_none
from content.models import ProviderState
from social_providers.models import CallKind
from social_providers.socialapi.budget import Priority
from social_providers.socialapi.client import CallClass
from social_providers.socialapi.connect import SocialAPIConnectAdapter, rows
from social_providers.socialapi.errors import SocialAPIError, to_platform_error
from social_providers.socialapi.publish_fields import BuiltRequest, build_request, refusals

#: The largest file `POST /media/upload` takes; bigger ones use the presigned path.
SERVER_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

_OCTET = "application/octet-stream"

#: Post statuses that mean nothing is live, so deleting the post is safe.
NEVER_LIVE = frozenset({"draft", "failed", "cancelled"})


def media_mode() -> str:
    """`url` when the vendor can fetch our storage's signed URLs; `upload`
    otherwise (local files, MinIO on localhost or a private network — the
    vendor rejects private and loopback addresses). `SOCIALAPI_MEDIA_MODE`
    forces either."""
    forced = str(getattr(settings, "SOCIALAPI_MEDIA_MODE", "") or "").lower()
    if forced in ("url", "upload"):
        return forced
    if getattr(settings, "USE_FAKE_STORAGE", True):
        return "upload"
    endpoint = str(getattr(settings, "AWS_S3_ENDPOINT_URL", "") or "")
    host = urlparse(endpoint).hostname or ""
    if not host or host == "localhost" or "." not in host:
        return "upload"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "url"
    return "url" if address.is_global else "upload"


def result_from_post(post: dict[str, Any]) -> PublishResult:
    """Our reading of one SocialAPI post with one target. Success is
    `published` **and** a platform post id (the vendor's own rule); a target
    still `publishing` or `pending` is pending, whatever the post says."""
    targets = post.get("targets") or [{}]
    target = targets[0] if isinstance(targets[0], dict) else {}
    pid = str(post.get("id") or "")
    status = str(target.get("status") or post.get("status") or "")
    platform_post_id = str(target.get("platform_post_id") or "")
    if status == "published" and platform_post_id:
        return PublishResult(
            provider_post_id=pid,
            platform_post_id=platform_post_id,
            permalink=str(target.get("permalink") or ""),
            published_at=parse_or_none(target.get("published_at") or post.get("published_at")),
        )
    if status == "failed" or post.get("status") in ("failed", "cancelled"):
        error = target.get("error") if isinstance(target.get("error"), dict) else {}
        return PublishResult(provider_post_id=pid, failed=True, error=dict(error or {}))
    return PublishResult(provider_post_id=pid, pending=True)


class SocialAPIAdapter(SocialAPIConnectAdapter):
    """The whole `PlatformAdapter` for SocialAPI: connect (inherited) and publish."""

    # --- publish -----------------------------------------------------------------------
    def publish(
        self,
        *,
        platform: str,
        provider_account_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        journal: PublishJournal | None = None,
        media: Sequence[PublishMedia] = (),
    ) -> PublishResult:
        if journal is None:
            # Without a journal a retry cannot tell a draft from a live post.
            raise PlatformError(
                "Publishing through SocialAPI needs a publish journal.",
                code="publish_journal_missing",
                retryable=False,
            )

        resumed = self._resume(journal)
        if resumed is not None:
            return resumed

        reasons = refusals(platform, payload)
        if reasons:
            raise PlatformError(" ".join(reasons), code="publish_refused", retryable=False)

        if journal.provider_state == ProviderState.DRAFT_CREATED and journal.provider_post_id:
            # The draft exists and the vendor still holds it (`_resume` read
            # it back as a draft): publish that one, never a second.
            return self._request_publish(journal.provider_post_id, journal)

        sources = self._media_sources(payload, media, journal)
        built = build_request(
            platform=platform,
            provider_account_id=provider_account_id,
            payload=payload,
            media_sources=sources,
        )
        self._validate(built)
        draft = self._create_draft(built, journal)
        self._assert_kept(draft, built)
        return self._request_publish(str(draft["id"]), journal)

    def _resume(self, journal: PublishJournal) -> PublishResult | None:
        """Where an earlier attempt left off. `None` to carry on publishing."""
        pid = journal.provider_post_id
        if not pid or journal.provider_state not in (
            ProviderState.DRAFT_CREATED,
            ProviderState.PUBLISH_REQUESTED,
        ):
            return None
        post = self._get_post(pid, priority=Priority.PUBLISH)
        if post is None:
            if journal.provider_state == ProviderState.PUBLISH_REQUESTED:
                raise PlatformError(
                    "The provider no longer has this post, so whether it went out cannot be "
                    "confirmed. Check the platform before publishing again.",
                    code="publish_unconfirmed",
                    detail={"provider_post_id": pid},
                )
            # A draft that is gone was never live: start again from nothing.
            journal.record(provider_post_id="", provider_state=ProviderState.NONE)
            return None
        still_a_draft = str(post.get("status") or "") in ("draft", "scheduled")
        state = ProviderState.DRAFT_CREATED if still_a_draft else ProviderState.PUBLISH_REQUESTED
        if journal.provider_state != state:
            journal.record(provider_state=state)
        return None if still_a_draft else result_from_post(post)

    def _media_sources(
        self, payload: dict[str, Any], media: Sequence[PublishMedia], journal: PublishJournal
    ) -> list[dict[str, str]]:
        wanted = [int(item["id"]) for item in payload.get("media") or [] if "id" in item]
        if not wanted:
            return []
        if [item.id for item in media] != wanted:
            raise PlatformError(
                "The media handed to the provider does not match the post's media.",
                code="publish_media_mismatch",
                detail={"payload": wanted, "given": [item.id for item in media]},
            )
        if media_mode() == "url":
            return [{"source_type": "url", "source": item.url} for item in media]

        # Upload, reusing whatever an earlier attempt already uploaded — each
        # id is journalled the moment it exists, so a crash mid-way leaks
        # nothing and re-uploads nothing.
        uploaded = list(journal.provider_media_ids)
        for item in media[len(uploaded) :]:
            uploaded.append(self._upload(item))
            journal.record(media_ids=uploaded, provider_state=ProviderState.MEDIA_UPLOADED)
        return [{"source_type": "media_id", "source": media_id} for media_id in uploaded]

    def _upload(self, item: PublishMedia) -> str:
        try:
            if item.size and item.size > SERVER_UPLOAD_MAX_BYTES:
                return self._upload_presigned(item)
            with item.open() as handle:
                answer = self.client.request(
                    "POST",
                    "/media/upload",
                    files={"file": (item.filename, handle, item.mime or _OCTET)},
                    priority=Priority.PUBLISH,
                    call_class=CallClass.MEDIA,
                )
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        media_id = str((answer.data or {}).get("media_id") or "")
        if not media_id:
            raise PlatformError("The provider accepted the upload but returned no media id.")
        return media_id

    def _upload_presigned(self, item: PublishMedia) -> str:
        slot = (
            self.client.get(
                "/media/upload-url",
                params={"media_type": item.mime, "filename": item.filename},
                priority=Priority.PUBLISH,
            ).data
            or {}
        )
        media_id, url = str(slot.get("media_id") or ""), str(slot.get("upload_url") or "")
        if not media_id or not url:
            raise PlatformError("The provider returned no upload address for a large file.")
        with item.open() as handle:
            self.client.put_external(
                url, content=_chunks(handle), content_type=item.mime, size=item.size
            )
        self.client.post(f"/media/{media_id}/verify", priority=Priority.PUBLISH)
        return media_id

    def _validate(self, built: BuiltRequest) -> None:
        try:
            answer = self.client.post(
                "/posts/validate", json=built.body, priority=Priority.PUBLISH
            ).data
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        answer = answer if isinstance(answer, dict) else {}
        errors = [row for row in answer.get("errors") or [] if isinstance(row, dict)]
        dropped = [
            row
            for row in answer.get("warnings") or []
            if isinstance(row, dict) and _names_a_key(str(row.get("field") or ""), built.data_keys)
        ]
        problems = errors + dropped
        if problems or (answer.get("valid") is False and not errors):
            messages = [str(row.get("message") or row.get("field") or "") for row in problems]
            raise PlatformError(
                "The platform would not accept this post: "
                + ("; ".join(m for m in messages if m) or "it did not pass validation."),
                code="publish_invalid",
                detail={"issues": problems[:10]},
            )

    def _create_draft(self, built: BuiltRequest, journal: PublishJournal) -> dict[str, Any]:
        try:
            answer = self.client.post("/posts", json=built.body, priority=Priority.PUBLISH)
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        post = answer.data if isinstance(answer.data, dict) else {}
        pid = str(post.get("id") or "")
        if not pid:
            raise PlatformError("The provider created the post but returned no id.")
        # Committed before anything else happens: from here on a retry finds
        # this draft instead of making another.
        journal.record(provider_post_id=pid, provider_state=ProviderState.DRAFT_CREATED)
        if str(post.get("status") or "draft") != "draft":
            # We never ask for publish_now or scheduled_at; anything else means
            # the request was not read the way we sent it.
            raise PlatformError(
                f"The provider stored the post as '{post.get('status')}', not as a draft.",
                code="publish_not_a_draft",
                detail={"provider_post_id": pid},
            )
        return post

    def _assert_kept(self, post: dict[str, Any], built: BuiltRequest) -> None:
        """G9: a `201` does not prove every field was applied. Compare what the
        vendor stored with what we sent; on a mismatch, delete the (never
        published) draft and refuse."""
        sent_target = built.body["targets"][0]
        stored_targets = [t for t in post.get("targets") or [] if isinstance(t, dict)]
        problems = []
        if str(post.get("text") or "") != built.body["text"]:
            problems.append("the text")
        if len(post.get("media") or []) != len(built.body.get("media") or []):
            problems.append("the media")
        if (
            len(stored_targets) != 1
            or stored_targets[0].get("account_id") != (sent_target["account_id"])
        ):
            problems.append("the account")
        if not problems:
            return
        pid = str(post.get("id") or "")
        with contextlib.suppress(PlatformError):  # else the reconciler sweeps it
            # `_create_draft` has just seen it stored as a draft: no read needed.
            self._delete(pid)
        raise PlatformError(
            f"The provider did not keep {', '.join(problems)} of this post, so it was not "
            "published.",
            code="publish_readback_mismatch",
            detail={"provider_post_id": pid, "lost": problems},
        )

    def _request_publish(self, pid: str, journal: PublishJournal) -> PublishResult:
        try:
            answer = self.client.post(f"/posts/{pid}/publish", priority=Priority.PUBLISH)
        except SocialAPIError as error:
            if error.kind == CallKind.CONFLICT:
                # Not publishable any more: an earlier request already went
                # through. Read where it stands; never ask again.
                journal.record(provider_state=ProviderState.PUBLISH_REQUESTED)
                return self._status(pid, priority=Priority.PUBLISH)
            if error.vendor_code == "publishing.delivery.all_failed":
                journal.record(provider_state=ProviderState.FAILED)
            raise to_platform_error(error) from error
        journal.record(provider_state=ProviderState.PUBLISH_REQUESTED)
        post = answer.data if isinstance(answer.data, dict) else {"id": pid}
        post.setdefault("id", pid)
        return result_from_post(post)

    # --- after the publish ------------------------------------------------------------
    def _get_post(self, pid: str, *, priority: str) -> dict[str, Any] | None:
        try:
            answer = self.client.get(f"/posts/{pid}", priority=priority)
        except SocialAPIError as error:
            if error.kind == CallKind.NOT_FOUND:
                return None
            raise to_platform_error(error) from error
        return answer.data if isinstance(answer.data, dict) else None

    def fetch_publish_status(self, *, provider_post_id: str) -> PublishResult:
        # The reconciler's read never competes with a publish for the budget (C-06).
        return self._status(provider_post_id, priority=Priority.BACKGROUND)

    def _status(self, pid: str, *, priority: str) -> PublishResult:
        post = self._get_post(pid, priority=priority)
        if post is None:
            return PublishResult.unconfirmed(
                pid, "The provider no longer has this post; check the platform."
            )
        return result_from_post(post)

    def discard_draft(self, *, provider_post_id: str) -> None:
        """Deletes a post **only** while nothing of it is live — `DELETE
        /posts/{id}` on a published post would delete it from the platforms."""
        post = self._get_post(provider_post_id, priority=Priority.BACKGROUND)
        if post is None:
            return
        status = str(post.get("status") or "")
        if status not in NEVER_LIVE:
            raise PlatformError(
                f"Refusing to delete a provider post that is '{status}'.",
                code="discard_refused",
                detail={"provider_post_id": provider_post_id},
            )
        self._delete(provider_post_id)

    def _delete(self, provider_post_id: str) -> None:
        """Only once the post is known never to have been live."""
        try:
            self.client.delete(f"/posts/{provider_post_id}", priority=Priority.BACKGROUND)
        except SocialAPIError as error:
            if error.kind != CallKind.NOT_FOUND:
                raise to_platform_error(error) from error

    def release_media(self, *, media_ids: Sequence[str]) -> list[str]:
        still_used: list[str] = []
        for media_id in media_ids:
            try:
                self.client.delete(f"/media/{media_id}", priority=Priority.BACKGROUND)
            except SocialAPIError as error:
                if error.kind == CallKind.NOT_FOUND:
                    continue
                if error.kind == CallKind.CONFLICT or error.retryable:
                    still_used.append(media_id)
                    continue
                raise to_platform_error(error) from error
        return still_used

    def stale_drafts(self, *, profile_id: str, created_before: dt.datetime) -> list[str]:
        """Drafts on our brand older than `created_before` — candidates for the
        orphan sweep (a crash between creating a draft and journalling it)."""
        try:
            answer = self.client.get(
                "/posts",
                params={
                    "brand_id": profile_id,
                    "status": "draft",
                    "sort": "created_asc",
                    "limit": 100,
                },
            ).data
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        stale = []
        for row in rows(answer):
            if row.get("status") != "draft":
                continue
            created = parse_or_none(row.get("created_at"))
            if created is not None and created < created_before and row.get("id"):
                stale.append(str(row["id"]))
        return stale


def _chunks(handle: IO[bytes], size: int = 1024 * 1024) -> Iterator[bytes]:
    """Streams a large file instead of holding it in the worker's memory."""
    while chunk := handle.read(size):
        yield chunk


def _names_a_key(field_name: str, keys: frozenset[str]) -> bool:
    """Whether a validation warning's `field` points at a platform_data key we
    sent (`platform_data.instagram.share_to_feed`, `share_to_feed`, …)."""
    parts = set(field_name.replace("[", ".").replace("]", "").split("."))
    return bool(parts & keys)
