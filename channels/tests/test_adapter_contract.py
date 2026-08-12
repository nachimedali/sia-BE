"""`PlatformAdapter` contract (design.md §9, A8, implementation.md Phase 9).

One suite, both implementations. The fake runs everywhere; `ZernioAdapter`
runs against `httpx.MockTransport`, so its **parameter assembly** is covered
without credentials — a missing `headless=true`, a misnamed `pageId`, a
forgotten `x-request-id` are exactly the bugs that never show up when only the
fake is tested, and exactly the ones that break D3's "swapping is a config
change". Mirrors `ai/tests/test_provider_contract.py`'s shape.

A run against live credentials is marked `integration` and skipped without
them (design.md §9).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from django.test import override_settings

from channels.adapters.base import PlatformError
from channels.adapters.fake import FakePlatformAdapter
from channels.adapters.zernio import ZernioAdapter, get_platform_adapter
from tests.httpx_transport import patch_httpx_transport


@pytest.fixture
def zernio(monkeypatch: Any) -> list[httpx.Request]:
    """A Zernio good enough to answer every call the contract makes."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path == "/api/v1/profiles":
            return httpx.Response(200, json={"_id": "prof-1"})
        if path.startswith("/api/v1/connect/") and path.endswith("/select-page"):
            if request.method == "GET":
                return httpx.Response(200, json={"pages": [{"id": "pg-1", "name": "Acme Page"}]})
            return httpx.Response(
                200,
                json={"account": {"accountId": "acct-1", "username": "acme", "displayName": "A"}},
            )
        if path.startswith("/api/v1/connect/"):
            return httpx.Response(200, json={"authUrl": "https://facebook.test/dialog/oauth"})
        if path.startswith("/api/v1/accounts/"):
            return httpx.Response(
                200, json={"username": "acme", "displayName": "Acme", "followerCount": 42}
            )
        if path == "/api/v1/posts":
            return httpx.Response(
                200,
                json={
                    "post": {
                        "_id": "zp-1",
                        "platforms": [{"platform": "instagram", "platformPostId": "ig-9"}],
                    }
                },
            )
        raise AssertionError(f"unexpected call to {path}")

    patch_httpx_transport(monkeypatch, handler)
    return captured


@pytest.fixture(params=["fake", "zernio"])
def adapter(request: Any, monkeypatch: Any) -> Any:
    if request.param == "fake":
        return FakePlatformAdapter()
    request.getfixturevalue("zernio")
    return ZernioAdapter()


# -----------------------------------------------------------------------------
# test_platform_adapter_contract
# -----------------------------------------------------------------------------
def test_platform_adapter_contract(adapter: Any) -> None:
    profile_id = adapter.ensure_profile(name="acme-studio")
    assert profile_id

    # Idempotent: handed a live profile it returns it rather than minting one.
    assert adapter.ensure_profile(name="acme-studio", existing_profile_id=profile_id) == profile_id

    auth_url = adapter.connect_url(
        platform="instagram", profile_id=profile_id, redirect_url="https://occs.test/cb"
    )
    assert auth_url.startswith("https://")

    resolution = adapter.resolve_callback(
        platform="instagram",
        profile_id=profile_id,
        params={"accountId": "acct-1", "code": "abc"},
    )
    assert resolution.needs_selection is False
    assert resolution.account is not None
    assert resolution.account.provider_account_id

    result = adapter.publish(
        platform="instagram",
        provider_account_id=resolution.account.provider_account_id,
        payload={"body": "Hello", "media": []},
        idempotency_key="key-1",
    )
    assert result.provider_post_id


def test_both_adapters_agree_on_which_platforms_need_a_selection_step(adapter: Any) -> None:
    """The one place a fake could silently diverge from the real thing and
    still pass everything else: taking the one-leg path where production takes
    two."""
    resolution = adapter.resolve_callback(
        platform="facebook", profile_id="prof-1", params={"tempToken": "t-1"}
    )

    assert resolution.needs_selection is True
    assert resolution.targets

    account = adapter.select_target(
        platform="facebook",
        profile_id="prof-1",
        params=resolution.params,
        target_id=resolution.targets[0].id,
    )
    assert account.provider_account_id


def test_both_adapters_reject_a_target_that_was_never_offered(adapter: Any) -> None:
    resolution = adapter.resolve_callback(
        platform="facebook", profile_id="prof-1", params={"tempToken": "t-1"}
    )

    with pytest.raises(PlatformError):
        adapter.select_target(
            platform="facebook",
            profile_id="prof-1",
            params=resolution.params,
            target_id="not-offered",
        )


# -----------------------------------------------------------------------------
# Zernio-specific wire assembly
# -----------------------------------------------------------------------------
def test_connect_url_requests_the_headless_flow(zernio: list[httpx.Request]) -> None:
    """D2's whole promise in one query parameter: without `headless=true` the
    user lands on a Zernio-branded selection screen (design.md §14, V1)."""
    ZernioAdapter().connect_url(
        platform="facebook", profile_id="prof-1", redirect_url="https://occs.test/cb"
    )

    request = zernio[-1]
    assert request.url.params["headless"] == "true"
    assert request.url.params["profileId"] == "prof-1"
    assert request.url.params["redirect_url"] == "https://occs.test/cb"


def test_publish_sends_the_idempotency_key_as_x_request_id(zernio: list[httpx.Request]) -> None:
    ZernioAdapter().publish(
        platform="instagram",
        provider_account_id="acct-1",
        payload={"body": "Hello", "media": []},
        idempotency_key="key-42",
    )

    request = zernio[-1]
    assert request.headers["x-request-id"] == "key-42"
    body = json.loads(request.content)
    assert body["publishNow"] is True
    assert body["platforms"] == [{"platform": "instagram", "accountId": "acct-1"}]


@override_settings(PUBLIC_MEDIA_BASE_URL="https://cdn.occs.test")
def test_publish_absolutises_relative_media_urls(zernio: list[httpx.Request]) -> None:
    """Zernio fetches media itself, so a storage-relative path is unusable —
    it would 404 on their side, not ours, which is the worst place to find
    out."""
    ZernioAdapter().publish(
        platform="instagram",
        provider_account_id="acct-1",
        payload={"body": "x", "media": [{"id": 1, "kind": "IMAGE", "url": "/media/a.png"}]},
        idempotency_key="key-1",
    )

    body = json.loads(zernio[-1].content)
    assert body["mediaItems"] == [{"type": "image", "url": "https://cdn.occs.test/media/a.png"}]


def test_publish_treats_a_409_with_an_existing_id_as_a_replay(monkeypatch: Any) -> None:
    """The V1 finding: a retry that outlived the provider's 5-minute
    `x-request-id` window lands on the 24-hour content-hash check instead, and
    that 409 means the post is already out (design.md §14)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"details": {"existingPostId": "zp-original"}})

    patch_httpx_transport(monkeypatch, handler)

    result = ZernioAdapter().publish(
        platform="instagram",
        provider_account_id="acct-1",
        payload={"body": "x", "media": []},
        idempotency_key="key-1",
    )

    assert result.provider_post_id == "zp-original"
    assert result.was_replay is True


def test_publish_treats_a_409_without_an_existing_id_as_a_failure(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": "duplicate"})

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError):
        ZernioAdapter().publish(
            platform="instagram",
            provider_account_id="acct-1",
            payload={"body": "x", "media": []},
            idempotency_key="key-1",
        )


def test_a_200_carrying_existing_post_is_reported_as_a_replay(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"existingPost": {"_id": "zp-original"}})

    patch_httpx_transport(monkeypatch, handler)

    result = ZernioAdapter().publish(
        platform="instagram",
        provider_account_id="acct-1",
        payload={"body": "x", "media": []},
        idempotency_key="key-1",
    )

    assert result.was_replay is True


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (400, False), (403, False)],
)
def test_failures_are_classified_for_the_back_off(
    monkeypatch: Any, status: int, retryable: bool
) -> None:
    """The publish task branches on `retryable` alone, so getting this wrong
    means either hammering a permanently-broken request or giving up on a
    transient one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"}, headers={"Retry-After": "30"})

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError) as caught:
        ZernioAdapter().publish(
            platform="instagram",
            provider_account_id="acct-1",
            payload={"body": "x", "media": []},
            idempotency_key="key-1",
        )

    assert caught.value.retryable is retryable
    assert caught.value.retry_after == (30 if status == 429 else None)


def test_a_lapsed_platform_grant_is_classified_as_needing_reauth(monkeypatch: Any) -> None:
    """Zernio's own error code, translated here and nowhere else — the publish
    task marks the account `REAUTH_REQUIRED` off `needs_reauth`, so a provider
    that renamed this string must break this test rather than silently make the
    task retry into a dead grant."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "ACCOUNT_DISCONNECTED"})

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError) as caught:
        ZernioAdapter().publish(
            platform="instagram",
            provider_account_id="acct-1",
            payload={"body": "x", "media": []},
            idempotency_key="key-1",
        )

    assert caught.value.needs_reauth is True
    assert caught.value.retryable is False


def test_get_platform_adapter_resolves_the_fake_under_the_test_settings() -> None:
    assert isinstance(get_platform_adapter(), FakePlatformAdapter)


@override_settings(USE_FAKE_PLATFORM_ADAPTER=False)
def test_get_platform_adapter_resolves_zernio_when_configured() -> None:
    assert isinstance(get_platform_adapter(), ZernioAdapter)


@pytest.mark.integration
def test_zernio_live_contract() -> None:  # pragma: no cover - needs credentials
    """The same contract against the real provider. Skipped without
    credentials, which is every environment except a deliberate one."""
    from django.conf import settings

    if not settings.ZERNIO_API_KEY:
        pytest.skip("ZERNIO_API_KEY not configured")

    adapter = ZernioAdapter()
    profile_id = adapter.ensure_profile(name="occs-contract-test")
    assert adapter.connect_url(
        platform="instagram", profile_id=profile_id, redirect_url=settings.SITE_URL
    ).startswith("https://")


# -----------------------------------------------------------------------------
# LinkedIn's second shape: a one-time token instead of inline choices
# -----------------------------------------------------------------------------
def test_linkedin_spends_the_pending_data_token_once_and_carries_the_result(
    monkeypatch: Any,
) -> None:
    """LinkedIn's organisation list is too large for the redirect's query
    string, so Zernio hands over a token that works exactly once. The
    resolution has to carry `tempToken` and `userProfile` forward, because
    `select_target` can never read them again."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/connect/pending-data":
            assert request.url.params["token"] == "pending-1"
            return httpx.Response(
                200,
                json={
                    "tempToken": "AQX-live",
                    "userProfile": {"id": "u-1", "displayName": "Jordan"},
                    "organizations": [
                        {"id": "12345", "urn": "urn:li:organization:12345", "name": "Acme Corp"}
                    ],
                },
            )
        return httpx.Response(
            200, json={"account": {"accountId": "acct-li", "username": "acme-corp"}}
        )

    patch_httpx_transport(monkeypatch, handler)
    adapter = ZernioAdapter()

    resolution = adapter.resolve_callback(
        platform="linkedin", profile_id="prof-1", params={"pendingDataToken": "pending-1"}
    )

    assert resolution.needs_selection is True
    assert [t.name for t in resolution.targets] == ["Acme Corp"]
    assert resolution.params["tempToken"] == "AQX-live"
    assert resolution.params["userProfile"]["id"] == "u-1"

    account = adapter.select_target(
        platform="linkedin", profile_id="prof-1", params=resolution.params, target_id="12345"
    )

    assert account.provider_account_id == "acct-li"
    assert calls.count("/api/v1/connect/pending-data") == 1, "the one-time token was spent twice"


def test_linkedin_selection_sends_the_organisation_urn(monkeypatch: Any) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"account": {"accountId": "acct-li"}})

    patch_httpx_transport(monkeypatch, handler)

    ZernioAdapter().select_target(
        platform="linkedin",
        profile_id="prof-1",
        params={
            "tempToken": "AQX",
            "userProfile": {"id": "u-1"},
            "targets": [{"id": "1", "urn": "urn:li:organization:1", "name": "Acme"}],
        },
        target_id="1",
    )

    body = json.loads(captured[-1].content)
    assert body["accountType"] == "organization"
    assert body["selectedOrganization"] == {
        "id": "1",
        "urn": "urn:li:organization:1",
        "name": "Acme",
    }


def test_selecting_on_a_platform_with_no_selection_step_is_refused(zernio: Any) -> None:
    with pytest.raises(PlatformError, match="without a selection step"):
        ZernioAdapter().select_target(
            platform="instagram",
            profile_id="prof-1",
            params={"targets": [{"id": "1", "name": "x"}]},
            target_id="1",
        )


def test_a_callback_with_no_account_id_is_refused(zernio: Any) -> None:
    with pytest.raises(PlatformError, match="no account id"):
        ZernioAdapter().resolve_callback(
            platform="instagram", profile_id="prof-1", params={"code": "abc"}
        )


def test_hydrating_a_connected_account_reads_its_follower_count(
    zernio: list[httpx.Request],
) -> None:
    resolution = ZernioAdapter().resolve_callback(
        platform="instagram", profile_id="prof-1", params={"accountId": "acct-1"}
    )

    assert resolution.account is not None
    assert resolution.account.followers == 42
    assert zernio[-1].url.path == "/api/v1/accounts/acct-1"


def test_disconnect_deletes_the_account_at_the_provider(monkeypatch: Any) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    patch_httpx_transport(monkeypatch, handler)

    ZernioAdapter().disconnect(provider_account_id="acct-1")

    assert captured[-1].method == "DELETE"
    assert captured[-1].url.path == "/api/v1/accounts/acct-1"


def test_a_profile_response_without_an_id_is_refused(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError, match="without returning its id"):
        ZernioAdapter().ensure_profile(name="acme")


def test_a_connect_response_without_an_auth_url_is_refused(monkeypatch: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError, match="no authorisation URL"):
        ZernioAdapter().connect_url(
            platform="instagram", profile_id="prof-1", redirect_url="https://occs.test/cb"
        )


def test_a_non_json_error_body_still_classifies(monkeypatch: Any) -> None:
    """Providers return HTML from their edge under load; that must not turn a
    retryable 503 into a crash inside the error handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="<html>gateway</html>")

    patch_httpx_transport(monkeypatch, handler)

    with pytest.raises(PlatformError) as caught:
        ZernioAdapter().disconnect(provider_account_id="acct-1")

    assert caught.value.retryable is True
