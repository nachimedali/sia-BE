"""Real `PlatformAdapter`: Zernio (D3, verified by V1 — design.md §14).

`httpx`, called synchronously, exactly like `ai/providers/llm_text.py`: every
caller runs inside a Celery task already (design.md §11), so nothing here
benefits from async.

**Why the connect flow is three calls, not one.** V1 resolved to headless mode
(`headless=true`), so the provider never renders a screen the user can see.
`connect_url` starts OAuth; the platform redirects the user back to *our*
page; `resolve_callback` turns those query params into either a finished
account or a list of pages/organisations we render ourselves; `select_target`
attaches the one the user picked. Only Facebook and LinkedIn take the second
leg (`SELECTION_PLATFORMS`) — Instagram (via Instagram Login), TikTok, YouTube
and Threads land connected on the first.

**Why publish tolerates a 409.** Zernio de-duplicates on the `x-request-id`
header for ~5 minutes and then falls through to a 24-hour content-hash check
that answers **409** carrying `details.existingPostId`. A publish task that
backs off past five minutes — which is exactly what a 429 or a 5xx makes it do
— lands in the 409 path rather than the 200 path, so a 409 with an existing id
is a *success* here: the post is out, and this call is the replay that proves
it. Treating it as a failure is how I9 would break in production while passing
every test that never waited five minutes (design.md §14, V1 finding 1).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from django.conf import settings

from channels.adapters.base import (
    SELECTION_PLATFORMS,
    ConnectedAccount,
    ConnectResolution,
    ConnectTarget,
    PlatformAdapter,
    PlatformError,
    PublishResult,
    echo_targets,
    find_offered_target,
)
from content.models import MediaKind

#: Zernio's `type` vocabulary for a media item, keyed by ours.
_MEDIA_TYPES = {MediaKind.IMAGE: "image", MediaKind.VIDEO: "video"}

#: Zernio's error code for "the platform grant behind this account is gone".
#: Translated into `PlatformError.needs_reauth` here so their vocabulary stops
#: at this module — the publish task reads the flag, never the string.
_DISCONNECTED_CODE = "ACCOUNT_DISCONNECTED"


def _client() -> Any:
    import httpx

    return httpx.Client(
        base_url=settings.ZERNIO_BASE_URL,
        headers={"Authorization": f"Bearer {settings.ZERNIO_API_KEY}"},
        timeout=settings.ZERNIO_TIMEOUT_SECONDS,
    )


def _raise_for(response: Any, *, label: str) -> None:
    """One classifier for every call. 429 and 5xx are worth another attempt;
    everything else is the provider telling us this request is wrong and will
    stay wrong."""
    if response.status_code < 400:
        return

    retry_after: float | None = None
    if response.status_code == 429:
        header = response.headers.get("Retry-After")
        retry_after = float(header) if header and header.isdigit() else None

    body = response.text[:500]
    code = ""
    try:
        parsed = response.json()
        code = parsed.get("code", "") if isinstance(parsed, dict) else ""
    except ValueError:
        parsed = None

    raise PlatformError(
        f"{label} returned {response.status_code}.",
        detail={"status": response.status_code, "body": body, "provider_code": code},
        retryable=response.status_code == 429 or response.status_code >= 500,
        retry_after=retry_after,
        needs_reauth=code == _DISCONNECTED_CODE,
    )


def _json(response: Any, *, label: str) -> dict[str, Any]:
    _raise_for(response, label=label)
    parsed: dict[str, Any] = response.json()
    return parsed


def _absolute(url: str) -> str:
    """Zernio fetches media itself, so a storage-relative path is unusable to
    it. Local/dev media is served relatively; S3 already yields an absolute
    URL and passes through untouched."""
    if url.startswith(("http://", "https://")):
        return url
    return str(urljoin(settings.PUBLIC_MEDIA_BASE_URL, url))


class ZernioAdapter:
    # --- profiles --------------------------------------------------------
    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        if existing_profile_id:
            return existing_profile_id
        with _client() as client:
            payload = _json(
                client.post("/v1/profiles", json={"name": name}), label="Zernio profiles"
            )
        profile_id = str(payload.get("_id") or payload.get("profile", {}).get("_id", ""))
        if not profile_id:
            raise PlatformError("Zernio created a profile without returning its id.")
        return profile_id

    # --- connect ---------------------------------------------------------
    def connect_url(self, *, platform: str, profile_id: str, redirect_url: str) -> str:
        with _client() as client:
            payload = _json(
                client.get(
                    f"/v1/connect/{platform}",
                    params={
                        "profileId": profile_id,
                        "redirect_url": redirect_url,
                        # The whole point of the headless flow: the user comes
                        # back to us, never to a Zernio-branded selector.
                        "headless": "true",
                    },
                ),
                label="Zernio connect",
            )
        auth_url = str(payload.get("authUrl", ""))
        if not auth_url:
            raise PlatformError("Zernio returned no authorisation URL.")
        return auth_url

    def resolve_callback(
        self, *, platform: str, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
        if platform not in SELECTION_PLATFORMS:
            # No selection step: the redirect already carries the account id.
            account_id = str(params.get("accountId", ""))
            if not account_id:
                raise PlatformError(
                    "The connect callback carried no account id.", detail={"platform": platform}
                )
            return ConnectResolution(account=self._hydrate(account_id))
        if platform == "facebook":
            return self._facebook_targets(profile_id=profile_id, params=params)
        return self._linkedin_targets(params=params)

    def _facebook_targets(
        self, *, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
        temp_token = str(params.get("tempToken", ""))
        with _client() as client:
            payload = _json(
                client.get(
                    "/v1/connect/facebook/select-page",
                    params={"profileId": profile_id, "tempToken": temp_token},
                ),
                label="Zernio facebook pages",
            )
        targets = [
            ConnectTarget(id=str(page["id"]), name=str(page.get("name", "")), payload=page)
            for page in payload.get("pages", [])
        ]
        return ConnectResolution(targets=targets, params=echo_targets(params, targets))

    def _linkedin_targets(self, *, params: dict[str, Any]) -> ConnectResolution:
        # LinkedIn's organisation list is too large for the redirect's query
        # string, so Zernio hands over a one-time token instead. Spending it
        # here is why `ConnectResolution.params` has to carry the result
        # forward rather than let `select_target` re-read it.
        token = str(params.get("pendingDataToken", ""))
        with _client() as client:
            payload = _json(
                client.get("/v1/connect/pending-data", params={"token": token}),
                label="Zernio pending data",
            )
        targets = [
            ConnectTarget(id=str(org["id"]), name=str(org.get("name", "")), payload=org)
            for org in payload.get("organizations", [])
        ]
        resolved = {
            **params,
            "tempToken": payload.get("tempToken", params.get("tempToken", "")),
            "userProfile": payload.get("userProfile", params.get("userProfile", {})),
        }
        return ConnectResolution(targets=targets, params=echo_targets(resolved, targets))

    def select_target(
        self, *, platform: str, profile_id: str, params: dict[str, Any], target_id: str
    ) -> ConnectedAccount:
        if platform not in SELECTION_PLATFORMS:
            raise PlatformError(
                "This platform connects without a selection step.", detail={"platform": platform}
            )

        target = find_offered_target(params, target_id)
        temp_token = str(params.get("tempToken", ""))
        user_profile = params.get("userProfile", {})

        if platform == "facebook":
            body: dict[str, Any] = {
                "profileId": profile_id,
                "pageId": target_id,
                "tempToken": temp_token,
                "userProfile": user_profile,
            }
            path = "/v1/connect/facebook/select-page"
        else:
            body = {
                "profileId": profile_id,
                "tempToken": temp_token,
                "userProfile": user_profile,
                "accountType": "organization",
                "selectedOrganization": {
                    "id": target_id,
                    "urn": target.get("urn", f"urn:li:organization:{target_id}"),
                    "name": target.get("name", ""),
                },
            }
            path = "/v1/connect/linkedin/select-organization"

        with _client() as client:
            payload = _json(client.post(path, json=body), label="Zernio connect selection")

        account = payload.get("account", {})
        return ConnectedAccount(
            provider_account_id=str(account.get("accountId", "")),
            handle=str(account.get("username", "") or ""),
            display_name=str(account.get("displayName", "") or ""),
        )

    def _hydrate(self, account_id: str) -> ConnectedAccount:
        """The redirect carries an id and a username; the account record
        carries the display name and follower count the connections screen
        shows. One extra read at connect time, never on the publish path."""
        with _client() as client:
            payload = _json(
                client.get(f"/v1/accounts/{account_id}"), label="Zernio account"
            )
        account = payload.get("account", payload)
        return ConnectedAccount(
            provider_account_id=account_id,
            handle=str(account.get("username", "") or ""),
            display_name=str(account.get("displayName", "") or ""),
            followers=int(account.get("followerCount", 0) or 0),
        )

    # --- publish ---------------------------------------------------------
    def publish(
        self,
        *,
        platform: str,
        provider_account_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> PublishResult:
        body = {
            "content": payload.get("body", ""),
            "platforms": [{"platform": platform, "accountId": provider_account_id}],
            "mediaItems": [
                {"type": _MEDIA_TYPES.get(item["kind"], "image"), "url": _absolute(item["url"])}
                for item in payload.get("media", [])
            ],
            "publishNow": True,
        }
        with _client() as client:
            response = client.post(
                "/v1/posts", json=body, headers={"x-request-id": idempotency_key}
            )

        if response.status_code == 409:
            return self._replay_from_conflict(response)

        parsed = _json(response, label="Zernio publish")
        # A same-`x-request-id` retry inside the 5-minute window answers 200
        # with the original under `existingPost` rather than `post`.
        existing = parsed.get("existingPost")
        post = existing or parsed.get("post", {})
        return PublishResult(
            provider_post_id=str(post.get("_id", "")),
            platform_post_id=_platform_post_id(post, platform),
            was_replay=existing is not None,
        )

    @staticmethod
    def _replay_from_conflict(response: Any) -> PublishResult:
        details = (response.json() or {}).get("details", {})
        existing_id = str(details.get("existingPostId", ""))
        if not existing_id:
            # A 409 without an id is a genuine conflict we cannot resolve into
            # a published post — do not pretend it succeeded.
            raise PlatformError(
                "Zernio rejected this post as a duplicate but named no original.",
                detail={"body": response.text[:500]},
            )
        return PublishResult(provider_post_id=existing_id, was_replay=True)

    def disconnect(self, *, provider_account_id: str) -> None:
        with _client() as client:
            _raise_for(
                client.delete(f"/v1/accounts/{provider_account_id}"),
                label="Zernio disconnect",
            )


def _platform_post_id(post: dict[str, Any], platform: str) -> str:
    for entry in post.get("platforms", []):
        if entry.get("platform") == platform:
            return str(entry.get("platformPostId", "") or "")
    return ""


def get_platform_adapter() -> PlatformAdapter:
    """Resolves the configured adapter. Swapping providers — Zernio for
    bundle.social, D3's stated fallback — is a settings change (A8)."""
    if getattr(settings, "USE_FAKE_PLATFORM_ADAPTER", False):
        from channels.adapters.fake import _fake_adapter

        return _fake_adapter
    return ZernioAdapter()
