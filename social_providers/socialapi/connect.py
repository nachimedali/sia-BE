"""SocialAPI's half of the publishing port that connects accounts (SA-20, SA-21).

`channels.adapters.base.PlatformAdapter` minus `publish` (SA-32): brand per
workspace, the connect link, the return from the platform, Facebook's and
Google's "choose which one" step, disconnect, and Pinterest boards.

**Where SocialAPI differs from Zernio, and what we do about it**
(`social-provider-plan.md` §2, §4.1):

* **A brand is our workspace** (L-1), and `POST /brands` is not idempotent —
  a retried create bills a second unit. So the brand is looked up by a name
  carrying the workspace's identifier before any create, and the id is
  persisted by the caller immediately (G1).
* **The return carries `status`**: `success` (one account), `error` (the user
  said no, or the platform refused), or `selection_required` — always for
  Facebook, for Google when one login manages several Business Profiles. A
  pending selection is single-use and lives 30 minutes; an expired one says
  "start again", never 500.
* **LinkedIn connects directly** here (Zernio made it pick an organisation).
* **X needs the customer's own developer app** (vendor policy), so it is
  refused with a sentence instead of a link that would fail on the other side.
* **No `GET /accounts/{id}`**: an account's details come from the brand's
  account list.
"""

from __future__ import annotations

from typing import Any

from channels.adapters.base import (
    ConnectedAccount,
    ConnectResolution,
    ConnectTarget,
    PlatformError,
    PublishResult,
    echo_targets,
    find_offered_target,
)
from social_providers.models import CallKind
from social_providers.socialapi.budget import Priority
from social_providers.socialapi.capabilities import COMMENTS
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.errors import SocialAPIError, to_platform_error

#: Our platform value → SocialAPI's connector slug. A platform absent here is
#: one SocialAPI cannot connect through its managed apps.
PLATFORM_SLUGS: dict[str, str] = {
    "instagram": "instagram",
    "facebook": "facebook",
    "threads": "threads",
    "tiktok": "tiktok",
    "linkedin": "linkedin",
    "youtube": "youtube",
    "pinterest": "pinterest",
    "google_business": "google",
}

#: Platforms SocialAPI pauses for a choice before an account exists.
SELECTION_PLATFORMS = frozenset({"facebook", "google_business"})

#: Platforms that cannot be connected through SocialAPI's managed apps, and why.
UNSUPPORTED: dict[str, str] = {
    "x": (
        "X needs your own X developer app on this provider, which ComposeVision does not "
        "set up yet. Use reminders for X for now."
    ),
}

BRAND_PREFIX = "cv:"


def brand_name(workspace_slug: str) -> str:
    """`cv:<workspace slug>` — the slug is unique, so the name is too, and an
    exact match finds a brand a crashed create already made."""
    return f"{BRAND_PREFIX}{workspace_slug}"


class SocialAPIConnectAdapter:
    """Implements every `PlatformAdapter` method; `publish` lands with SA-32."""

    selection_platforms = SELECTION_PLATFORMS
    unsupported = UNSUPPORTED
    comment_platforms = frozenset(platform for platform, read in COMMENTS.items() if read)
    #: The return must carry the `state` we signed (`channels.services`).
    requires_state = True

    def __init__(self, client: SocialAPIClient | None = None) -> None:
        self.client = client or SocialAPIClient()

    # --- the brand ---------------------------------------------------------------------
    def ensure_profile(self, *, name: str, existing_profile_id: str = "") -> str:
        """`name` is the workspace slug. An existing id is checked against the
        vendor, so a brand deleted in the dashboard is noticed instead of
        failing every connect; otherwise the brand is found by its exact name
        before one is created."""
        try:
            listing = self.client.get("/brands", priority=Priority.PUBLISH).data or {}
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        brands = listing.get("data") if isinstance(listing, dict) else listing
        brands = brands if isinstance(brands, list) else []
        ids = {str(row.get("id")) for row in brands if isinstance(row, dict)}
        if existing_profile_id and existing_profile_id in ids:
            return existing_profile_id
        wanted = brand_name(name)
        for row in brands:
            if isinstance(row, dict) and str(row.get("name", "")) == wanted:
                return str(row["id"])
        # Every vendor account starts with an empty "Default" brand that already
        # costs a unit (Free has 2). Adopting it keeps the second unit free for
        # a second workspace instead of spending both on one.
        for row in brands:
            if (
                isinstance(row, dict)
                and str(row.get("name", "")) == "Default"
                and not row.get("accounts_count")
            ):
                try:
                    self.client.patch(
                        f"/brands/{row['id']}", json={"name": wanted}, priority=Priority.PUBLISH
                    )
                except SocialAPIError as error:
                    raise to_platform_error(error) from error
                return str(row["id"])
        try:
            created = self.client.post("/brands", json={"name": wanted}, priority=Priority.PUBLISH)
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        brand_id = str((created.data or {}).get("id") or "")
        if not brand_id:
            raise PlatformError("SocialAPI created a brand but returned no id.")
        return brand_id

    # --- the link ------------------------------------------------------------------------
    def connect_url(
        self, *, platform: str, profile_id: str, redirect_url: str, state: str = ""
    ) -> str:
        if platform in UNSUPPORTED:
            raise PlatformError(UNSUPPORTED[platform], code="platform_not_connectable")
        slug = PLATFORM_SLUGS.get(platform)
        if slug is None:
            raise PlatformError(
                f"{platform} cannot be connected through this provider.",
                code="platform_not_connectable",
            )
        try:
            answer = self.client.post(
                "/accounts/connect",
                json={
                    "platform": slug,
                    "redirect_uri": redirect_url,
                    "state": state,
                    "brand_id": profile_id,
                },
                priority=Priority.PUBLISH,
            )
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        auth_url = str((answer.data or {}).get("auth_url") or "")
        if not auth_url.startswith("https://"):
            raise PlatformError("SocialAPI did not return a sign-in link.")
        return auth_url

    # --- the return ------------------------------------------------------------------------
    def resolve_callback(
        self, *, platform: str, profile_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
        status = str(params.get("status") or "")
        if status == "error":
            reason = str(params.get("error_description") or params.get("error") or "")
            denied = params.get("error") == "access_denied"
            raise PlatformError(
                "The sign-in was cancelled, so nothing was connected."
                if denied
                else f"The platform refused the connection: {reason or 'no reason given'}.",
                code="connect_denied" if denied else "connect_failed",
                detail={"error": str(params.get("error") or "")},
            )
        if status == "selection_required":
            connection_id = str(params.get("connection_id") or "")
            if not connection_id:
                raise PlatformError("The selection step is missing its connection id.")
            return self._pending(platform, connection_id, params)
        if status == "success":
            account_id = str(params.get("account_id") or "")
            if not account_id:
                raise PlatformError("The platform said yes but no account came back.")
            return ConnectResolution(account=self._account(profile_id, account_id))
        raise PlatformError(
            "The connection did not come back from the platform.",
            code="connect_failed",
            detail={"status": status},
        )

    def _pending(
        self, platform: str, connection_id: str, params: dict[str, Any]
    ) -> ConnectResolution:
        try:
            pending = (
                self.client.get(
                    f"/accounts/pending/{connection_id}", priority=Priority.PUBLISH
                ).data
                or {}
            )
        except SocialAPIError as error:
            if error.kind == CallKind.NOT_FOUND:
                raise PlatformError(
                    "This connection expired (they last 30 minutes). Start it again.",
                    code="connect_expired",
                ) from error
            raise to_platform_error(error) from error
        targets: list[ConnectTarget] = []
        for page in pending.get("pages") or []:
            if not isinstance(page, dict):
                continue
            assignable = bool(page.get("assignable", True))
            note = (
                ""
                if assignable
                else f" · already used by {page.get('assigned_brand_name') or 'another brand'}"
            )
            targets.append(
                ConnectTarget(
                    id=str(page.get("platform_page_id", "")),
                    name=f"{page.get('name', '')}{note}",
                    payload={"kind": "page", "assignable": assignable},
                )
            )
        for profile in pending.get("profiles") or []:
            if isinstance(profile, dict):
                targets.append(
                    ConnectTarget(
                        id=str(profile.get("platform_account_id", "")),
                        name=str(profile.get("display_name") or profile.get("username") or ""),
                        payload={"kind": "profile", "assignable": True},
                    )
                )
        targets = [target for target in targets if target.id]
        if not targets:
            raise PlatformError(
                "This login manages no Page or profile we can connect.", code="connect_no_targets"
            )
        state = {**params, "connection_id": connection_id}
        return ConnectResolution(targets=targets, params=echo_targets(state, targets))

    def select_target(
        self, *, platform: str, profile_id: str, params: dict[str, Any], target_id: str
    ) -> ConnectedAccount:
        offered = find_offered_target(params, target_id)
        if offered.get("assignable") is False:
            raise PlatformError(
                "That Page is already connected to another brand.", code="target_unavailable"
            )
        connection_id = str(params.get("connection_id") or "")
        body = (
            {"page_ids": [target_id]}
            if offered.get("kind") == "page"
            else {"platform_account_id": target_id}
        )
        try:
            answer = self.client.post(
                f"/accounts/pending/{connection_id}/select", json=body, priority=Priority.PUBLISH
            )
        except SocialAPIError as error:
            if error.kind == CallKind.NOT_FOUND:
                raise PlatformError(
                    "This connection expired (they last 30 minutes). Start it again.",
                    code="connect_expired",
                ) from error
            if error.vendor_code == "platform.facebook.page_cap":
                raise PlatformError(
                    "This brand already has as many Facebook Pages as the provider allows.",
                    code="page_cap",
                ) from error
            raise to_platform_error(error) from error
        account_id = str((answer.data or {}).get("account_id") or "")
        if not account_id:
            raise PlatformError("The platform said yes but no account came back.")
        return self._account(profile_id, account_id, fallback=answer.data or {})

    def _account(
        self, brand_id: str, account_id: str, *, fallback: dict[str, Any] | None = None
    ) -> ConnectedAccount:
        """The account's handle and name from the brand's account list (there
        is no single-account read). Falls back to the connect answer's names."""
        try:
            listing = (
                self.client.get(
                    "/accounts", params={"brand_id": brand_id}, priority=Priority.PUBLISH
                ).data
                or {}
            )
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        rows = listing.get("data") if isinstance(listing, dict) else listing
        row = next(
            (r for r in rows or [] if isinstance(r, dict) and str(r.get("id")) == account_id),
            fallback or {},
        )
        metadata = row.get("metadata") or {}
        followers = metadata.get("follower_count") or metadata.get("subscriber_count") or 0
        username = str(row.get("username") or "")
        return ConnectedAccount(
            provider_account_id=account_id,
            handle=f"@{username}" if username and not username.startswith("@") else username,
            display_name=str(
                row.get("page_name") or row.get("name") or row.get("display_name") or ""
            ),
            followers=int(followers) if str(followers).isdigit() else 0,
        )

    # --- after ------------------------------------------------------------------------------
    def disconnect(self, *, provider_account_id: str) -> None:
        """Hard delete at the vendor. Already gone counts as done."""
        try:
            self.client.delete(f"/accounts/{provider_account_id}", priority=Priority.PUBLISH)
        except SocialAPIError as error:
            if error.kind in (CallKind.NOT_FOUND, CallKind.REAUTH) and error.status == 404:
                return
            raise to_platform_error(error) from error

    def list_remote_options(
        self, *, platform: str, provider_account_id: str, source: str
    ) -> list[dict[str, Any]]:
        if source != "pinterest_boards" or platform != "pinterest":
            return []
        try:
            boards = self.client.get(f"/accounts/{provider_account_id}/boards").data or {}
        except SocialAPIError as error:
            raise to_platform_error(error) from error
        rows = boards.get("data") if isinstance(boards, dict) else boards
        return [
            {"id": str(row.get("board_id") or row.get("id")), "name": str(row.get("name", ""))}
            for row in rows or []
            if isinstance(row, dict) and (row.get("board_id") or row.get("id"))
        ]

    def publish(
        self,
        *,
        platform: str,
        provider_account_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> PublishResult:
        from social_providers.registry import ProviderNotReadyError

        raise ProviderNotReadyError(
            "Publishing through SocialAPI lands with SA-32.", detail={"port": "publish"}
        )
