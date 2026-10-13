"""The audience through SocialAPI: comments read, answered and moderated (SA-60/61/62).

* **Reading** pages a post's comments newest first and stops at the first
  comment already held (plan G11 — the listing has no `since`), at most
  `MAX_PAGES` pages per read. The `comment.received` webhook writes through the
  same `ingest.store_comments`, so a comment arriving both ways is one row.
* **No reaction breakdown** exists at this vendor (G5): reactions are `None` —
  "not shared" — never `{}` and never zeros (SA-61).
* **Every comment carries the platform's own capability flags**
  (`can_reply`, `can_hide`, `can_like`, `can_delete`, `can_private_reply`);
  they are stored on the row and the actions here are offered only where they
  say yes (SA-62).
* **Replies spend the vendor's interaction allowance** (Free: 50 a month) as
  well as ours: `interaction_allowance` reads it so the stricter of the two
  refuses first, in its own words.

Platform post ids, not SocialAPI's: the inbox routes take the platform's id of
the published post and the connected account that owns it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from analytics.providers.base import (
    CommentSnapshot,
    MetricsError,
    MetricsUnsupportedError,
)
from common.timestamps import parse_or_none
from social_providers.models import CallKind
from social_providers.socialapi.budget import Priority
from social_providers.socialapi.capabilities import COMMENTS
from social_providers.socialapi.client import CallClass, SocialAPIClient
from social_providers.socialapi.connect import rows
from social_providers.socialapi.errors import SocialAPIError, to_metrics_error

PROVIDER_KEY = "socialapi"

#: Comments per page, and pages per read: a busy post's backlog arrives over
#: several cadences instead of one read spending the minute's budget.
PAGE_SIZE = 50
MAX_PAGES = 5

#: Our action → (method, path suffix, carries the account in the body).
ACTIONS: dict[str, tuple[str, str, bool]] = {
    "hide": ("POST", "/hide", True),
    "unhide": ("DELETE", "/hide", False),
    "like": ("POST", "/like", True),
    "unlike": ("DELETE", "/like", False),
    "delete": ("DELETE", "", False),
}

_FLAGS = ("can_reply", "can_hide", "can_like", "can_delete", "can_private_reply")


class SocialAPIAudienceProvider:
    key = PROVIDER_KEY

    def __init__(self, client: SocialAPIClient | None = None) -> None:
        self.client = client or SocialAPIClient()

    def supports_comments(self, platform: str) -> bool:
        return COMMENTS.get(platform, False)

    # --- reading -----------------------------------------------------------------------
    def fetch_comments(
        self,
        *,
        platform: str,
        provider_post_id: str,
        since: dt.datetime | None = None,
        platform_post_id: str = "",
        provider_account_id: str = "",
    ) -> list[CommentSnapshot]:
        if not self.supports_comments(platform):
            raise MetricsUnsupportedError(
                "This platform shares no comments through the provider.",
                detail={"platform": platform},
            )
        if not platform_post_id or not provider_account_id:
            raise MetricsError(
                "The post has no platform id yet, so its comments cannot be read.",
                detail={"provider_post_id": provider_post_id},
            )
        found: list[CommentSnapshot] = []
        cursor = ""
        for _page in range(MAX_PAGES):
            params = {"account_id": provider_account_id, "limit": str(PAGE_SIZE)}
            if cursor:
                params["cursor"] = cursor
            try:
                answer = self.client.get(f"/inbox/comments/{platform_post_id}", params=params).data
            except SocialAPIError as error:
                raise to_metrics_error(error) from error
            page = [snapshot for snapshot in map(_snapshot, rows(answer)) if snapshot.external_id]
            fresh = [
                snapshot
                for snapshot in page
                if since is None or snapshot.posted_at is None or snapshot.posted_at > since
            ]
            found += fresh
            pagination = answer.get("pagination") if isinstance(answer, dict) else None
            cursor = str((pagination or {}).get("next_cursor") or "")
            # G11: stop at the first page made entirely of comments already
            # held — the listing has no `since`, and order is the platform's.
            if (page and not fresh) or not cursor or not (pagination or {}).get("has_more"):
                break
        return found

    def fetch_reactions(self, *, platform: str, provider_post_id: str) -> dict[str, int] | None:
        """No reaction breakdown at this vendor (G5): not shared, never zero."""
        return None

    # --- acting ------------------------------------------------------------------------
    def reply(
        self,
        *,
        platform: str,
        comment_external_id: str,
        body: str,
        platform_post_id: str = "",
        provider_account_id: str = "",
    ) -> str:
        """Posts a threaded reply. Spends one vendor interaction."""
        try:
            answer = self.client.post(
                f"/inbox/comments/{platform_post_id}",
                json={
                    "account_id": provider_account_id,
                    "comment_id": comment_external_id,
                    "text": body,
                },
                priority=Priority.PUBLISH,
            ).data
        except SocialAPIError as error:
            raise to_metrics_error(error) from error
        return str((answer or {}).get("comment_id") or "")

    def moderate(
        self,
        *,
        action: str,
        comment_external_id: str,
        platform_post_id: str,
        provider_account_id: str,
    ) -> None:
        """Hide, unhide, like, unlike or delete one comment on the platform."""
        method, suffix, in_body = ACTIONS[action]
        path = f"/inbox/comments/{platform_post_id}/{comment_external_id}{suffix}"
        try:
            if in_body:
                self.client.post(path, json={"account_id": provider_account_id})
            else:
                self.client.request(
                    method,
                    path,
                    params={"account_id": provider_account_id},
                    priority=Priority.BACKGROUND,
                    call_class=CallClass.WRITE,
                )
        except SocialAPIError as error:
            if action == "delete" and error.kind == CallKind.NOT_FOUND:
                return  # already gone: the outcome asked for
            raise to_metrics_error(error) from error

    def interaction_allowance(self) -> tuple[int, int] | None:
        """`(used, limit)` of the vendor's monthly interactions, `None` when
        unlimited or not reported."""
        try:
            usage = self.client.get("/usage").data or {}
        except SocialAPIError as error:
            raise to_metrics_error(error) from error
        used, limit = usage.get("interactions_used"), usage.get("interactions_limit")
        if not isinstance(used, int) or not isinstance(limit, int) or limit < 0:
            return None
        return used, limit


def _snapshot(row: dict[str, Any]) -> CommentSnapshot:
    capabilities = row.get("capabilities")
    return CommentSnapshot(
        external_id=str(row.get("id") or ""),
        body=str(row.get("text") or ""),
        author=str(row.get("author_name") or row.get("author_username") or ""),
        posted_at=parse_or_none(row.get("created_at")),
        reactions=None,
        capabilities=(
            {flag: bool(capabilities.get(flag)) for flag in _FLAGS}
            if isinstance(capabilities, dict)
            else None
        ),
    )
