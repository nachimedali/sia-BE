"""SocialAPI's errors, classified by code (SA-11) — one case per row of plan §3.3.

What matters is what a caller does next, so each case asserts the kind and the
two flags the publish task branches on. The two 429s are the reason this table
exists: same status, opposite handling.
"""

from __future__ import annotations

import pytest

from analytics.providers.base import MetricsError, MetricsUnsupportedError
from channels.adapters.base import PlatformError
from social_providers.models import CallKind
from social_providers.socialapi.errors import (
    SocialAPIError,
    classify,
    to_metrics_error,
    to_platform_error,
)


@pytest.mark.parametrize(
    ("status", "code", "kind", "retryable", "reauth"),
    [
        # the two 429s
        (429, "ratelimit.exceeded", CallKind.RATE_LIMITED, True, False),
        (429, "validation.post_limit_exhausted", CallKind.QUOTA_EXHAUSTED, False, False),
        (429, "validation.interaction_limit_exhausted", CallKind.QUOTA_EXHAUSTED, False, False),
        (400, "billing.post_limit", CallKind.QUOTA_EXHAUSTED, False, False),
        (413, "billing.storage_quota", CallKind.STORAGE_FULL, False, False),
        # the account
        (403, "account.reconnection_required", CallKind.REAUTH, False, True),
        (404, "account.not_found", CallKind.REAUTH, False, True),
        (400, "account.no_active_page", CallKind.REAUTH, False, True),
        (403, "platform.instagram.insights_scope_missing", CallKind.SCOPE_MISSING, False, False),
        (403, "platform.facebook.page_cap", CallKind.QUOTA_EXHAUSTED, False, False),
        # our key and setup
        (401, "auth.invalid_key", CallKind.MISCONFIGURED, False, False),
        (401, "auth.missing_header", CallKind.MISCONFIGURED, False, False),
        (403, "auth.insufficient_scope", CallKind.MISCONFIGURED, False, False),
        (400, "byok.missing", CallKind.MISCONFIGURED, False, False),
        # the request
        (400, "validation.field_invalid", CallKind.INVALID, False, False),
        (409, "post.state_invalid", CallKind.CONFLICT, False, False),
        (409, "media.in_use", CallKind.CONFLICT, False, False),
        (404, "post.not_found", CallKind.NOT_FOUND, False, False),
        (410, "resource.gone", CallKind.NOT_FOUND, False, False),
        (501, "resource.not_supported", CallKind.UNSUPPORTED, False, False),
        # the vendor
        (500, "system.internal", CallKind.UNAVAILABLE, True, False),
        (502, "", CallKind.UNAVAILABLE, True, False),
        (504, "publishing.delivery.timeout", CallKind.UNAVAILABLE, True, False),
        (None, "", CallKind.UNAVAILABLE, True, False),
        # the upstream platform, by status
        (401, "platform.tiktok.token_expired", CallKind.REAUTH, False, True),
        (429, "platform.instagram.rate_limited", CallKind.RATE_LIMITED, True, False),
        (400, "platform.youtube.title_too_long", CallKind.INVALID, False, False),
        (503, "platform.facebook.unavailable", CallKind.UNAVAILABLE, True, False),
        # an unknown code falls back to its status, never to a guess
        (418, "something.new", CallKind.INVALID, False, False),
    ],
)
def test_classification(
    status: int | None, code: str, kind: str, retryable: bool, reauth: bool
) -> None:
    assert classify(status, code) == kind
    error = SocialAPIError("x", kind=kind, status=status, vendor_code=code)
    assert error.retryable is retryable
    assert error.needs_reauth is reauth


def test_the_publish_port_sees_retryable_and_reauth() -> None:
    limited = to_platform_error(
        SocialAPIError(
            "slow down", kind=CallKind.RATE_LIMITED, status=429, retry_after=34.0, request_id="r1"
        )
    )
    assert isinstance(limited, PlatformError)
    assert (limited.retryable, limited.retry_after, limited.needs_reauth) == (True, 34.0, False)
    assert limited.payload["request_id"] == "r1"

    reauth = to_platform_error(SocialAPIError("reconnect", kind=CallKind.REAUTH, status=403))
    assert isinstance(reauth, PlatformError)
    assert (reauth.retryable, reauth.needs_reauth) == (False, True)

    exhausted = to_platform_error(
        SocialAPIError("month used", kind=CallKind.QUOTA_EXHAUSTED, status=429)
    )
    assert isinstance(exhausted, PlatformError)
    assert exhausted.retryable is False
    assert exhausted.code == "socialapi_quota_exhausted"


def test_the_metrics_port_records_unsupported_as_unavailable() -> None:
    unsupported = to_metrics_error(SocialAPIError("no", kind=CallKind.UNSUPPORTED, status=501))
    assert isinstance(unsupported, MetricsUnsupportedError)
    failed = to_metrics_error(SocialAPIError("down", kind=CallKind.UNAVAILABLE, status=503))
    assert isinstance(failed, MetricsError) and not isinstance(failed, MetricsUnsupportedError)
    assert failed.retryable is True
