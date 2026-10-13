"""SocialAPI's errors, classified by `error.code` — never by message, rarely by status.

The vendor's envelope is `{"error": {"code", "message", "meta"}, "request_id"}`
(docs: *Error Handling*). The code's namespace says where a failure came from;
two answers with the same HTTP status can need opposite handling:

* `429 ratelimit.exceeded` — the team's per-minute budget. Wait `Retry-After`.
* `429 validation.post_limit_exhausted` — the Free plan's month is used up.
  Waiting a minute changes nothing; retrying for a month is the bug.

Every call site branches on `SocialAPIError.kind` and the derived flags. The
mapping is a table (`CODE_KINDS`, `PREFIX_KINDS`, `STATUS_KINDS`) so a new code
the vendor documents is a row, and an unknown one still lands on the status
fallback rather than on a guess.

`to_platform_error` / `to_metrics_error` translate into the ports' own
exceptions, so the publish task and the capture ladder keep branching on what
they already understand (`retryable`, `needs_reauth`, `MetricsUnsupportedError`).
"""

from __future__ import annotations

from typing import Any

from common.exceptions import ProviderError
from social_providers.models import CallKind

#: Exact codes, checked first.
CODE_KINDS: dict[str, str] = {
    "ratelimit.exceeded": CallKind.RATE_LIMITED,
    "validation.post_limit_exhausted": CallKind.QUOTA_EXHAUSTED,
    "validation.interaction_limit_exhausted": CallKind.QUOTA_EXHAUSTED,
    "billing.post_limit": CallKind.QUOTA_EXHAUSTED,
    "billing.storage_quota": CallKind.STORAGE_FULL,
    "account.reconnection_required": CallKind.REAUTH,
    "account.not_found": CallKind.REAUTH,
    "account.no_active_page": CallKind.REAUTH,
    "account.page_inactive": CallKind.REAUTH,
    "account.different_login": CallKind.CONFLICT,
    "account.already_linked": CallKind.CONFLICT,
    "account.unsupported_platform": CallKind.UNSUPPORTED,
    "account.pages_not_supported": CallKind.UNSUPPORTED,
    "platform.instagram.insights_scope_missing": CallKind.SCOPE_MISSING,
    "platform.facebook.page_cap": CallKind.QUOTA_EXHAUSTED,
    "post.state_invalid": CallKind.CONFLICT,
    "post.no_retryable_deliveries": CallKind.CONFLICT,
    "media.in_use": CallKind.CONFLICT,
    "resource.conflict": CallKind.CONFLICT,
    "resource.not_found": CallKind.NOT_FOUND,
    "resource.gone": CallKind.NOT_FOUND,
    "resource.not_supported": CallKind.UNSUPPORTED,
    "system.internal": CallKind.UNAVAILABLE,
    "publishing.delivery.timeout": CallKind.UNAVAILABLE,
    "auth.insufficient_scope": CallKind.MISCONFIGURED,
}

#: Namespaces, checked when the exact code is not listed. Longest prefix wins.
PREFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("auth.", CallKind.MISCONFIGURED),
    ("byok.", CallKind.MISCONFIGURED),
    ("validation.", CallKind.INVALID),
    ("billing.", CallKind.QUOTA_EXHAUSTED),
    ("publishing.delivery.", CallKind.UNAVAILABLE),
)

#: Last resort: the HTTP status, for unknown codes and `platform.<name>.*`
#: (the upstream platform's own error, whose meaning follows its status).
STATUS_KINDS: dict[int, str] = {
    400: CallKind.INVALID,
    401: CallKind.MISCONFIGURED,
    403: CallKind.REAUTH,
    404: CallKind.NOT_FOUND,
    409: CallKind.CONFLICT,
    410: CallKind.NOT_FOUND,
    413: CallKind.STORAGE_FULL,
    422: CallKind.INVALID,
    429: CallKind.RATE_LIMITED,
    501: CallKind.UNSUPPORTED,
}

#: Kinds a caller may simply try again later.
RETRYABLE_KINDS = frozenset({CallKind.RATE_LIMITED, CallKind.UNAVAILABLE})


def classify(status: int | None, code: str) -> str:
    """The kind for a status and vendor code. `status=None` is a call that
    never got an answer (timeout, connection refused)."""
    if status is None:
        return CallKind.UNAVAILABLE
    if code in CODE_KINDS:
        return CODE_KINDS[code]
    if not code.startswith("platform."):
        for prefix, kind in sorted(PREFIX_KINDS, key=lambda row: -len(row[0])):
            if code.startswith(prefix):
                return kind
    if status >= 500 and status != 501:
        return CallKind.UNAVAILABLE
    if code.startswith("platform.") and status == 401:
        # The platform refused the account's grant, not our key.
        return CallKind.REAUTH
    return STATUS_KINDS.get(status, CallKind.INVALID if status < 500 else CallKind.UNAVAILABLE)


class SocialAPIError(ProviderError):
    """One failed SocialAPI call, classified."""

    default_code = "socialapi_error"
    default_detail = "The social provider could not complete this request."

    def __init__(
        self,
        message: str | None = None,
        *,
        kind: str,
        status: int | None = None,
        vendor_code: str = "",
        request_id: str = "",
        retry_after: float | None = None,
        local: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> None:
        detail = {
            "kind": kind,
            "status": status,
            "vendor_code": vendor_code,
            "request_id": request_id,
            **({"retry_after": retry_after} if retry_after is not None else {}),
            **({"local": True} if local else {}),
        }
        super().__init__(message, detail=detail, code=f"socialapi_{str(kind).lower()}")
        self.kind = kind
        self.status = status
        self.vendor_code = vendor_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.local = local
        self.meta = meta or {}

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def needs_reauth(self) -> bool:
        return self.kind == CallKind.REAUTH


def to_platform_error(error: SocialAPIError) -> Exception:
    """The publish port's exception. `retryable` and `needs_reauth` are the only
    things the publish task branches on (`channels.adapters.base`)."""
    from channels.adapters.base import PlatformError

    return PlatformError(
        error.message,
        detail=dict(error.payload),
        code=error.code,
        retryable=error.retryable,
        retry_after=error.retry_after,
        needs_reauth=error.needs_reauth,
    )


def to_metrics_error(error: SocialAPIError) -> Exception:
    """The measurement port's exception: unsupported is recorded as unavailable
    and never retried; everything else is a failed call that may succeed later."""
    from analytics.providers.base import MetricsError, MetricsUnsupportedError

    if error.kind == CallKind.UNSUPPORTED:
        return MetricsUnsupportedError(error.message, detail=dict(error.payload))
    return MetricsError(
        error.message,
        detail=dict(error.payload),
        code=error.code,
        retryable=error.retryable,
        retry_after=error.retry_after,
    )
