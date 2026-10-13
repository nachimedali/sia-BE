"""The one HTTP client every SocialAPI adapter uses (`social-provider-plan.md` §3).

Every call goes through the same six steps, so no adapter can skip one:

1. **Key present, or refuse before the wire.** A blank key reaching the vendor
   comes back as `401 auth.missing_header` and would read as an outage.
2. **Budget.** A token from the team-wide budget at the call's priority
   (`budget.TeamBudget`); a refusal is a local `RATE_LIMITED` error carrying
   how long to wait. Publishing may spend the reserve; background may not.
3. **Send**, with a timeout per call class (reads, writes, media).
4. **Retry only what is safe**: GETs, on `UNAVAILABLE` (transport failure,
   5xx), twice, with backoff. **A write is never retried here** — a duplicate
   post or reply is worse than a failed one, and the callers that can make a
   write safe to repeat (the publish journal, SA-32) do so themselves.
5. **Learn** the budget from `X-RateLimit-*`, whatever the outcome.
6. **Log** the call to `ProviderCall`: every write and failure, a sample of
   successful reads. Path normalised, no body, no key; the vendor's request id
   always, because it is what their support needs.

A 2xx whose body is not JSON is `BAD_RESPONSE`, not success: the vendor's own
docs warn that a `201` does not prove every field was applied, and a body we
cannot read proves even less.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction

from social_providers.models import CallKind, ProviderCall
from social_providers.socialapi.budget import Priority, RateState, TeamBudget, parse_headers
from social_providers.socialapi.errors import SocialAPIError, classify

logger = logging.getLogger(__name__)

PROVIDER = "socialapi"


class CallClass:
    READ = "read"
    WRITE = "write"
    MEDIA = "media"


#: Seconds. A write gets the configured timeout; reads are kept short so a
#: slow vendor cannot hold a capture worker; uploads get room for the bytes.
def _timeout(call_class: str) -> float:
    if call_class == CallClass.READ:
        return min(15.0, float(settings.SOCIALAPI_TIMEOUT_SECONDS))
    if call_class == CallClass.MEDIA:
        return max(120.0, float(settings.SOCIALAPI_TIMEOUT_SECONDS))
    return float(settings.SOCIALAPI_TIMEOUT_SECONDS)


READ_RETRIES = 2
BACKOFF_SECONDS = (0.5, 1.5)

#: A path segment that is an id rather than a word: digits, or a vendor prefix.
_ID_SEGMENT = re.compile(r"\d|^(acc|sapi|pc|cred|wh|br|post|p|b|mt)_", re.I)


def normalise_path(path: str) -> str:
    """`/posts/7c9e…/metrics` → `/posts/{id}/metrics`. The log stores shapes,
    never identifiers."""
    clean = path.split("?", 1)[0]
    return "/".join(
        "{id}" if segment and _ID_SEGMENT.search(segment) else segment
        for segment in clean.split("/")
    )[:200]


@dataclass(frozen=True)
class SocialAPIResponse:
    status: int
    data: Any
    request_id: str
    rate: RateState
    headers: dict[str, str] = field(default_factory=dict)


class SocialAPIClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        budget: TeamBudget | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sample: Callable[[], float] = random.random,
    ) -> None:
        self.api_key = settings.SOCIALAPI_API_KEY if api_key is None else api_key
        self.base_url = (base_url or settings.SOCIALAPI_BASE_URL).rstrip("/")
        self._transport = transport
        self._budget = budget or TeamBudget()
        self._sleep = sleep
        self._sample = sample

    # --- verbs ----------------------------------------------------------------------
    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        priority: str = Priority.BACKGROUND,
    ) -> SocialAPIResponse:
        return self.request(
            "GET", path, params=params, priority=priority, call_class=CallClass.READ
        )

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        priority: str = Priority.BACKGROUND,
        call_class: str = CallClass.WRITE,
    ) -> SocialAPIResponse:
        return self.request("POST", path, json=json, priority=priority, call_class=call_class)

    def patch(
        self, path: str, *, json: Any = None, priority: str = Priority.BACKGROUND
    ) -> SocialAPIResponse:
        return self.request("PATCH", path, json=json, priority=priority, call_class=CallClass.WRITE)

    def delete(self, path: str, *, priority: str = Priority.BACKGROUND) -> SocialAPIResponse:
        return self.request("DELETE", path, priority=priority, call_class=CallClass.WRITE)

    def put_external(self, url: str, *, content: Any, content_type: str, size: int = 0) -> None:
        """PUT bytes to a presigned storage URL the vendor handed out (the
        large-file upload path). Not a SocialAPI API call — no key, no budget
        — but the same transport, so tests see it. `content` may be an
        iterator of chunks, streamed rather than held in memory."""
        headers = {"Content-Type": content_type}
        if size:
            headers["Content-Length"] = str(size)
        try:
            with httpx.Client(timeout=_timeout(CallClass.MEDIA), transport=self._transport) as http:
                response = http.put(url, content=content, headers=headers)
        except httpx.HTTPError as exc:
            raise SocialAPIError(
                f"The media upload could not be sent ({type(exc).__name__}).",
                kind=classify(None, ""),
            ) from exc
        if response.status_code >= 400:
            raise SocialAPIError(
                f"The media upload was refused ({response.status_code}).",
                kind=classify(response.status_code, ""),
                status=response.status_code,
            )

    # --- the one path every call takes ------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        priority: str = Priority.BACKGROUND,
        call_class: str = CallClass.READ,
    ) -> SocialAPIResponse:
        shape = normalise_path(path)
        if not self.api_key:
            error = SocialAPIError(
                "SocialAPI has no API key (SOCIALAPI_API_KEY).",
                kind=CallKind.MISCONFIGURED,
                local=True,
            )
            self._log(method, shape, call_class, priority, None, error=error, latency=0, attempts=0)
            raise error

        retries = READ_RETRIES if method == "GET" else 0
        attempt = 0
        while True:
            attempt += 1
            allowance = self._budget.acquire(priority)
            if not allowance:
                error = SocialAPIError(
                    "SocialAPI request budget is spent for this minute.",
                    kind=CallKind.RATE_LIMITED,
                    retry_after=round(allowance.retry_after, 2),
                    local=True,
                )
                self._log(
                    method,
                    shape,
                    call_class,
                    priority,
                    None,
                    error=error,
                    latency=0,
                    attempts=attempt,
                )
                raise error

            started = time.monotonic()
            try:
                response = self._send(
                    method, path, params=params, json=json, files=files, call_class=call_class
                )
            except httpx.HTTPError as exc:
                latency = int((time.monotonic() - started) * 1000)
                error = SocialAPIError(
                    f"SocialAPI could not be reached ({type(exc).__name__}).",
                    kind=CallKind.UNAVAILABLE,
                )
                if attempt <= retries:
                    self._sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
                    continue
                self._log(
                    method,
                    shape,
                    call_class,
                    priority,
                    None,
                    error=error,
                    latency=latency,
                    attempts=attempt,
                )
                raise error from exc

            latency = int((time.monotonic() - started) * 1000)
            rate = parse_headers(response.headers)
            self._budget.observe(rate, status=response.status_code)
            request_id = response.headers.get("X-Request-ID", "")

            if response.status_code < 400:
                try:
                    data = response.json() if response.content else None
                except ValueError:
                    error = SocialAPIError(
                        "SocialAPI answered with a body that is not JSON.",
                        kind=CallKind.BAD_RESPONSE,
                        status=response.status_code,
                        request_id=request_id,
                    )
                    self._log(
                        method,
                        shape,
                        call_class,
                        priority,
                        response.status_code,
                        error=error,
                        latency=latency,
                        attempts=attempt,
                        rate=rate,
                    )
                    raise error from None
                result = SocialAPIResponse(
                    status=response.status_code,
                    data=data,
                    request_id=request_id,
                    rate=rate,
                    headers=dict(response.headers),
                )
                self._log(
                    method,
                    shape,
                    call_class,
                    priority,
                    response.status_code,
                    error=None,
                    latency=latency,
                    attempts=attempt,
                    rate=rate,
                    request_id=request_id,
                )
                return result

            error = self._error_from(response, request_id, rate)
            if error.kind == CallKind.UNAVAILABLE and attempt <= retries:
                self._sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
                continue
            self._log(
                method,
                shape,
                call_class,
                priority,
                response.status_code,
                error=error,
                latency=latency,
                attempts=attempt,
                rate=rate,
            )
            raise error

    # --- internals -------------------------------------------------------------------
    def _send(
        self, method: str, path: str, *, params: Any, json: Any, files: Any, call_class: str
    ) -> httpx.Response:
        with httpx.Client(
            base_url=self.base_url,
            timeout=_timeout(call_class),
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "composevision/1.0",
            },
            follow_redirects=False,
        ) as http:
            return http.request(method, path, params=params, json=json, files=files)

    @staticmethod
    def _error_from(response: httpx.Response, request_id: str, rate: RateState) -> SocialAPIError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        envelope = body.get("error") if isinstance(body, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        code = str(envelope.get("code") or "")
        message = str(envelope.get("message") or f"SocialAPI answered {response.status_code}.")
        if not request_id and isinstance(body, dict):
            request_id = str(body.get("request_id") or "")
        retry_after = None
        if response.status_code == 429:
            header = response.headers.get("Retry-After")
            retry_after = float(header) if header and header.isdigit() else rate.reset
        return SocialAPIError(
            message[:300],
            kind=classify(response.status_code, code),
            status=response.status_code,
            vendor_code=code,
            request_id=request_id,
            retry_after=retry_after,
            meta=envelope.get("meta") if isinstance(envelope.get("meta"), dict) else None,
        )

    def _log(
        self,
        method: str,
        shape: str,
        call_class: str,
        priority: str,
        status: int | None,
        *,
        error: SocialAPIError | None,
        latency: int,
        attempts: int,
        rate: RateState | None = None,
        request_id: str = "",
    ) -> None:
        """Write the call log row. Never raises — a logging failure must not
        turn a successful publish into a failed one."""
        is_read_success = error is None and method == "GET"
        if is_read_success and self._sample() >= float(settings.SOCIALAPI_READ_SAMPLE_RATE):
            return
        try:
            # Its own savepoint: a failed insert must not poison the caller's
            # transaction (a publish mid-commit, a capture batch).
            with transaction.atomic():
                ProviderCall.objects.create(
                    provider=PROVIDER,
                    method=method,
                    path=shape,
                    call_class=call_class,
                    priority=priority,
                    status_code=status,
                    kind=error.kind if error else CallKind.OK,
                    error_code=error.vendor_code[:80] if error else "",
                    request_id=(error.request_id if error else request_id)[:80],
                    latency_ms=max(0, latency),
                    attempts=max(1, attempts),
                    rate_limit=rate.limit if rate else None,
                    rate_remaining=rate.remaining if rate else None,
                    local=bool(error and error.local),
                )
        except Exception:  # the log is best-effort by design
            logger.warning("provider call log write failed", exc_info=True)
        if error is not None:
            logger.warning(
                "socialapi call failed",
                extra={
                    "path": shape,
                    "status": status,
                    "kind": error.kind,
                    "vendor_code": error.vendor_code,
                    "request_id": error.request_id,
                },
            )
