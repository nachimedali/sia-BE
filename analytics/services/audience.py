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

from analytics.models import AudienceComment
from billing.models import ReactionDetail
from billing.services import ledger
from billing.services.entitlements import entitlements_for
from workspaces.models import Workspace

REPLY_FEATURE = "reply_to_comments"


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


def reply_to(comment: AudienceComment, *, body: str, actor: Any) -> str:
    """Send one reply, debiting the org's pooled allowance (P0-36).

    Order matters and is deliberate: entitlement, then debit, then the
    provider call. Debiting first means an org that is out of allowance never
    reaches the vendor; calling first would spend real money before finding
    out we could not account for it. The debit's lock never spans the network
    call — `debit_reply` closes its transaction before this returns to it.
    """
    from analytics.providers import get_metrics_registry

    workspace = comment.post_target.post.workspace
    entitlements = entitlements_for(workspace)
    entitlements.require_feature(REPLY_FEATURE)

    ledger.debit_reply(workspace, actor=actor, note=f"comment {comment.external_id}")

    provider = get_metrics_registry().for_comments(comment.post_target.platform)
    if provider is None:
        from analytics.providers.base import MetricsUnsupportedError

        raise MetricsUnsupportedError(
            "This platform cannot accept a reply through the provider.",
            detail={"platform": comment.post_target.platform},
        )
    return str(
        provider.reply(
            platform=comment.post_target.platform,
            comment_external_id=comment.external_id,
            body=body,
        )
    )
