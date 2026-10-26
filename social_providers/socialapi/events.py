"""What each SocialAPI webhook event does here (SA-42, plan §7).

**Ownership first.** Every event names an account or a post; it must resolve to
one of ours (`provider_key="socialapi"`), or it is dropped as foreign and its
payload is not kept (plan §0.5 — another tenant's audience is not ours to
store). Then:

* `post.published` / `post.partial` / `post.failed`: read the outcome from
  the provider now (`reconcile.settle_target`) — the event says *something
  changed*, the provider's record says what.
* `post.unpublished` / `post.deleted`: a live target becomes `removed`
  (capture stops); a draft we discarded ourselves is nothing.
* `account.disconnected`: reconnect required → `REAUTH_REQUIRED`; revoked on
  the platform → `REVOKED`; the admins are told once.
* `page.removed`: `PAGE_REMOVED`; the admins are told once.
* `comment.received`: stored through the one comment write path, on plans
  whose capture is the webhook (L-4a); polled on the others.
* `mention.received`: kept for the S2 inbox.
* anything else: ignored, ownership still checked.

Every handler is idempotent: the vendor retries, and the receiver may hand the
same delivery to a second worker after a crash.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from analytics.providers.base import CommentSnapshot
from channels.models import SocialAccount, SocialAccountStatus
from common.timestamps import parse_or_none
from content.models import PostTarget, PostTargetState, ProviderState
from social_providers.models import DeliveryStatus

logger = logging.getLogger(__name__)

PROVIDER = "socialapi"

#: `(status, note)` — what became of a delivery.
Outcome = tuple[str, str]

#: The vendor's `reason` on `account.disconnected`, in a sentence a person
#: can act on. An unknown reason falls back to the generic one.
DISCONNECT_REASONS: dict[str, str] = {
    "all_pages_removed": "Every Page on this account was removed or is no longer yours.",
    "access_revoked": "Access to this account was revoked on the platform.",
    "number_banned": "The platform banned this number; reconnecting waits on an appeal.",
}
REVOKED_REASON = "This account was disconnected on the platform's side."


def route(event: str, payload: dict[str, Any]) -> Outcome:
    data = payload.get("data")
    if not isinstance(data, dict):
        return DeliveryStatus.IGNORED, "The event carried no data."
    handler = HANDLERS.get(event)
    if handler is None:
        return _seen(data, f"{event} is not used here.")
    return handler(data)


# --- ownership ---------------------------------------------------------------------------
def _accounts(account_id: Any) -> list[SocialAccount]:
    if not account_id:
        return []
    return list(
        SocialAccount.objects.filter(
            provider_account_id=str(account_id), provider_key=PROVIDER
        ).select_related("workspace")
    )


def _targets(post_id: Any) -> list[PostTarget]:
    if not post_id:
        return []
    return list(
        PostTarget.objects.filter(
            provider_post_id=str(post_id), provider_key=PROVIDER
        ).select_related("post__workspace", "post__author", "social_account")
    )


FOREIGN: Outcome = (DeliveryStatus.FOREIGN, "Names no account or post of ours.")


def _seen(data: dict[str, Any], note: str) -> Outcome:
    """An event with nothing to do, still held to the ownership check."""
    if _accounts(data.get("account_id")) or _targets(data.get("post_id")):
        return DeliveryStatus.IGNORED, note
    return FOREIGN


# --- posts -------------------------------------------------------------------------------
def _post_outcome(data: dict[str, Any]) -> Outcome:
    from scheduling import reconcile

    targets = _targets(data.get("post_id"))
    if not targets:
        return FOREIGN
    settled = sum(1 for target in targets if reconcile.settle_target(target))
    if settled:
        return DeliveryStatus.PROCESSED, f"{settled} target(s) settled from the provider."
    return DeliveryStatus.IGNORED, "Already settled, or the provider is still delivering."


def _post_removed(data: dict[str, Any]) -> Outcome:
    from scheduling.journal import TargetJournal

    targets = _targets(data.get("post_id"))
    if not targets:
        return FOREIGN
    account_id = data.get("account_id")
    removed = 0
    for target in targets:
        if target.state != PostTargetState.PUBLISHED or target.provider_state == (
            ProviderState.REMOVED
        ):
            continue  # a draft we discarded ourselves, or already recorded
        account = target.social_account
        if account_id and account is not None and account.provider_account_id != str(account_id):
            continue  # unpublished from another target of the same post
        TargetJournal(target).record(provider_state=ProviderState.REMOVED)
        removed += 1
    if removed:
        return DeliveryStatus.PROCESSED, f"{removed} live target(s) marked removed."
    return DeliveryStatus.IGNORED, "Nothing live was removed."


# --- accounts ----------------------------------------------------------------------------
def _set_state(accounts: list[SocialAccount], status: str, reason: str) -> int:
    from channels.services import set_account_status

    return sum(set_account_status(account, status, reason) for account in accounts)


def _account_disconnected(data: dict[str, Any]) -> Outcome:
    accounts = _accounts(data.get("account_id"))
    if not accounts:
        return FOREIGN
    if data.get("reconnect_required"):
        reason = DISCONNECT_REASONS.get(str(data.get("reason") or ""), REVOKED_REASON)
        changed = _set_state(accounts, SocialAccountStatus.REAUTH_REQUIRED, reason)
    else:
        changed = _set_state(accounts, SocialAccountStatus.REVOKED, REVOKED_REASON)
    if changed:
        return DeliveryStatus.PROCESSED, f"{changed} account(s) need reconnecting."
    return DeliveryStatus.IGNORED, "Already recorded."


def _page_removed(data: dict[str, Any]) -> Outcome:
    accounts = _accounts(data.get("account_id"))
    if not accounts:
        return FOREIGN
    name = str(data.get("name") or "").strip()
    reason = (
        f"The Page {name} was removed, or you are no longer its admin."
        if name
        else "This Page was removed, or you are no longer its admin."
    )
    changed = _set_state(accounts, SocialAccountStatus.PAGE_REMOVED, reason)
    if changed:
        return DeliveryStatus.PROCESSED, "Marked as a removed Page."
    return DeliveryStatus.IGNORED, "Already recorded."


# --- the audience ------------------------------------------------------------------------
def _comment(data: dict[str, Any]) -> Outcome:
    from analytics.services.ingest import store_comments
    from billing.services.entitlements import entitlements_for

    accounts = _accounts(data.get("account_id"))
    if not accounts:
        return FOREIGN
    platform_post_id = str(data.get("platform_post_id") or "")
    if not platform_post_id:
        return DeliveryStatus.UNMATCHED, "No post named; the comment poll will find it."
    target = (
        PostTarget.objects.filter(
            social_account__in=accounts,
            platform_post_id=platform_post_id,
            state=PostTargetState.PUBLISHED,
        )
        .select_related("post__workspace__organization__plan")
        .first()
    )
    if target is None:
        return DeliveryStatus.IGNORED, "A post not published through ComposeVision."
    if entitlements_for(target.post.workspace).comment_capture_interval() is not None:
        # L-4a: near-real-time capture is what the top plan buys; other plans
        # read comments at their own cadence.
        return DeliveryStatus.IGNORED, "Comments are polled on this plan."
    author = _mapping(data.get("author"))
    content = _mapping(data.get("content"))
    snapshot = CommentSnapshot(
        external_id=str(data.get("id") or ""),
        body=str(content.get("text") or ""),
        author=str(author.get("name") or author.get("id") or ""),
        posted_at=parse_or_none(data.get("received_at")),
        reactions=None,
    )
    if not snapshot.external_id:
        return DeliveryStatus.IGNORED, "The comment carried no id."
    stored = store_comments(target, [snapshot])
    return DeliveryStatus.PROCESSED, "Stored." if stored else "Already held."


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mention(data: dict[str, Any]) -> Outcome:
    if not _accounts(data.get("account_id")):
        return FOREIGN
    return DeliveryStatus.KEPT, "Kept for the inbox (S2)."


HANDLERS: dict[str, Callable[[dict[str, Any]], Outcome]] = {
    "post.published": _post_outcome,
    "post.partial": _post_outcome,
    "post.failed": _post_outcome,
    "post.unpublished": _post_removed,
    "post.deleted": _post_removed,
    "account.disconnected": _account_disconnected,
    "page.removed": _page_removed,
    "comment.received": _comment,
    "mention.received": _mention,
}

#: Events subscribed at registration: what `HANDLERS` uses. DMs, reviews and
#: the rest stay unsubscribed (plan §7; reviews are S2's, SA-99).
SUBSCRIBED: tuple[str, ...] = (
    *HANDLERS,
    "account.connected",
)

#: Which queue each event's processing runs on: the publish pool for what
#: decides whether posts go out, the measurement pool for the audience.
LIFECYCLE = frozenset(
    {
        "post.published",
        "post.partial",
        "post.failed",
        "post.unpublished",
        "post.deleted",
        "account.disconnected",
        "page.removed",
        "account.connected",
    }
)
