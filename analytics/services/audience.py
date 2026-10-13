"""Audience-comment reads and replies (L-4a, P0-34..P0-36).

Two rules run through everything here:

* **Depth is tiered, and it degrades rather than fails.** A plan capped at
  `TOTAL` reactions sees a real total, not a fabricated breakdown and not an
  error. A plan without `reply_to_comments` reads and analyses exactly as
  before; it just replies in the native app.
* **Unavailable is not empty.** A TikTok target carries an unavailability
  marker, and every read here distinguishes "we cannot see this" from "there
  was nothing to see".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from analytics.models import AudienceComment, AudienceCommentAction, CommentAction
from billing.models import ReactionDetail
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from common.exceptions import OCCSError, ProviderError, QuotaExceeded
from workspaces.models import Workspace

REPLY_FEATURE = "reply_to_comments"

#: Which capability flag on a comment allows each action (the provider's own
#: names, kept with the comment).
CAPABILITY_FOR: dict[str, str] = {
    "reply": "can_reply",
    "hide": "can_hide",
    "unhide": "can_hide",
    "like": "can_like",
    "unlike": "can_like",
    "delete": "can_delete",
}


@dataclass(frozen=True)
class ReactionView:
    """A reaction breakdown as one plan is allowed to see it.

    `available=False` is a platform that does not break reactions down at all,
    which is a different statement from a post with no reactions — and the
    surface has to render them differently (L-3).
    """

    available: bool
    total: int | None
    by_type: dict[str, int] | None


def visible_reactions(raw: dict[str, int] | None, *, detail: str) -> ReactionView:
    """Applies the plan's reaction depth to a stored breakdown.

    `None` in means the platform does not report a breakdown; the answer is
    unavailable at every tier, because no plan buys data the vendor does not
    have.
    """
    if raw is None:
        return ReactionView(available=False, total=None, by_type=None)

    total = sum(raw.values())
    if detail == ReactionDetail.TOTAL:
        # A real total, not a fabricated breakdown. The lower tier is shown
        # less, never something untrue.
        return ReactionView(available=True, total=total, by_type=None)
    return ReactionView(available=True, total=total, by_type=dict(raw))


def comments_for(
    workspace: Workspace, *, now: dt.datetime | None = None
) -> QuerySet[AudienceComment]:
    """The comments this workspace may read, bounded by its history depth.

    Bounded here rather than at each call site so the horizon cannot drift
    between the feed, the sentiment aggregate and any later export. The
    horizon is `analytics_history_days`, reused rather than reinvented — L-4a
    says explicitly that comment retention rides on the same 7/90/730 ladder.
    """
    horizon = entitlements_for(workspace).analytics_horizon_days()
    cutoff = (now or timezone.now()) - dt.timedelta(days=horizon)
    return (
        AudienceComment.objects.measured()
        .filter(post_target__post__workspace=workspace, posted_at__gte=cutoff)
        .select_related("post_target")
    )


def target_has_audience(target: Any) -> bool:
    """Whether this platform reports comments at all.

    Read from the stored marker rather than recomputed from the provider, so
    the answer survives a vendor outage: "TikTok has no comment endpoint" is
    not something that becomes true and false with connectivity.
    """
    return not target.post_comments.filter(
        external_id__startswith="__comments_unavailable__"
    ).exists()


class CommentActionRefusedError(OCCSError):
    """The platform does not allow this on this comment (its own capability
    flags say no). 422: a provider limit, not a permission or a plan."""

    status_code = 422
    default_code = "comment_action_unsupported"
    default_detail = "The platform does not allow this on this comment."


def _allowed(comment: AudienceComment, action: str) -> None:
    """Refuses an action the comment's capability flags rule out. A comment
    whose provider gave no flags (Zernio) allows only the reply it always did."""
    flag = CAPABILITY_FOR[action]
    flags = comment.capabilities
    allowed = (action == CommentAction.REPLY) if flags is None else bool(flags.get(flag))
    if not allowed:
        raise CommentActionRefusedError(
            f"{comment.post_target.get_platform_display()} does not allow this on this comment.",
            detail={"action": action, "capability": flag},
        )


def _provider_for(comment: AudienceComment) -> Any:
    from analytics.providers import get_metrics_registry
    from analytics.providers.base import MetricsUnsupportedError

    provider = get_metrics_registry().for_comments(comment.post_target.platform)
    if provider is None:
        raise MetricsUnsupportedError(
            "This platform cannot be answered through the provider.",
            detail={"platform": comment.post_target.platform},
        )
    return provider


def _addresses(comment: AudienceComment) -> dict[str, str]:
    """What a provider needs to find the comment's post on the platform."""
    target = comment.post_target
    account = target.social_account
    return {
        "platform_post_id": target.platform_post_id,
        "provider_account_id": account.provider_account_id if account else "",
    }


def _check_provider_allowance(provider: Any) -> None:
    """The vendor's own monthly interaction allowance (Free: 50) — checked
    before our debit, so the stricter of the two refuses first and says whose
    allowance it was (plan §6)."""
    read = getattr(provider, "interaction_allowance", None)
    allowance = read() if read is not None else None
    if allowance is not None and allowance[0] >= allowance[1]:
        raise QuotaExceeded(
            "The social provider's reply allowance for this month is used up. Replies "
            "resume when it renews; you can still answer from the platform's own app.",
            code="provider_interactions_exhausted",
            detail={"used": allowance[0], "limit": allowance[1]},
        )


def reply_to(comment: AudienceComment, *, body: str, actor: Any) -> str:
    """Send one reply, debiting the org's pooled allowance (P0-36).

    Order matters and is deliberate: entitlement, the comment's own flags, the
    provider's allowance, then our debit, then the provider call. Debiting
    before the call means an org that is out of allowance never reaches the
    vendor; a call the vendor refuses is given back by a compensating ledger
    row. The debit's lock never spans the network call.
    """
    from analytics.providers.base import MetricsError

    workspace = comment.post_target.post.workspace
    entitlements_for(workspace).require_feature(REPLY_FEATURE)
    _allowed(comment, CommentAction.REPLY)
    provider = _provider_for(comment)
    _check_provider_allowance(provider)

    debit = ledger.debit_reply(workspace, actor=actor, note=f"comment {comment.external_id}")
    try:
        external_id = str(
            provider.reply(
                platform=comment.post_target.platform,
                comment_external_id=comment.external_id,
                body=body,
                **_addresses(comment),
            )
        )
    except MetricsError as error:
        ledger.refund_reply(debit, note=f"refused: comment {comment.external_id}")
        if error.payload.get("kind") == "QUOTA_EXHAUSTED":
            raise QuotaExceeded(
                "The social provider's reply allowance for this month is used up.",
                code="provider_interactions_exhausted",
            ) from error
        raise ProviderError(error.message, detail=error.payload, code=error.code) from error
    AudienceCommentAction.objects.create(
        comment=comment,
        action=CommentAction.REPLY,
        actor=actor,
        external_id=external_id,
        body=body,
        provider_key=getattr(provider, "key", ""),
    )
    return external_id


def moderate(comment: AudienceComment, *, action: str, actor: Any) -> CommentState:
    """Hide, show, like, unlike or delete one audience comment (SA-62).

    The same commercial gate as replying (answering the audience from inside
    the app is what the top plan buys) and the comment's own flags. Not
    metered: only a reply spends an interaction at the provider.
    """
    from analytics.providers.base import MetricsError

    if action not in MODERATION:
        raise CommentActionRefusedError("Unknown action.", detail={"action": action})
    workspace = comment.post_target.post.workspace
    entitlements_for(workspace).require_feature(REPLY_FEATURE)
    if state_of(comment).deleted:
        raise CommentActionRefusedError(
            "This comment was deleted.", code="comment_deleted", detail={"action": action}
        )
    _allowed(comment, action)
    provider = _provider_for(comment)
    act = getattr(provider, "moderate", None)
    if act is None:
        raise CommentActionRefusedError(
            "The provider cannot do this.", detail={"action": action, "provider": provider.key}
        )
    try:
        act(action=action, comment_external_id=comment.external_id, **_addresses(comment))
    except MetricsError as error:
        raise ProviderError(error.message, detail=error.payload, code=error.code) from error
    AudienceCommentAction.objects.create(
        comment=comment, action=action, actor=actor, provider_key=getattr(provider, "key", "")
    )
    return state_of(comment)


#: Actions `moderate` takes (a reply goes through `reply_to`).
MODERATION = frozenset(
    {
        CommentAction.HIDE,
        CommentAction.UNHIDE,
        CommentAction.LIKE,
        CommentAction.UNLIKE,
        CommentAction.DELETE,
    }
)


@dataclass(frozen=True)
class CommentState:
    hidden: bool
    liked: bool
    deleted: bool
    replies: int


def state_of(comment: AudienceComment) -> CommentState:
    """A comment's current state: its latest action of each kind."""
    # Through `.all()`, so a list view's `prefetch_related("actions")` serves it.
    actions = [row.action for row in sorted(comment.actions.all(), key=lambda row: row.created_at)]
    hidden = liked = deleted = False
    for action in actions:
        if action in (CommentAction.HIDE, CommentAction.UNHIDE):
            hidden = action == CommentAction.HIDE
        elif action in (CommentAction.LIKE, CommentAction.UNLIKE):
            liked = action == CommentAction.LIKE
        elif action == CommentAction.DELETE:
            deleted = True
    return CommentState(
        hidden=hidden,
        liked=liked,
        deleted=deleted,
        replies=sum(1 for action in actions if action == CommentAction.REPLY),
    )
