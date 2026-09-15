"""Campaign lifecycle and membership (P3-04, P3-05).

Business logic lives here and never in a serializer or a view, so a task, a
command or a later endpoint inherits the same rules.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from billing.services.entitlements import entitlements_for
from common.exceptions import StateConflict
from content.models import ContentKind, Post
from planning.models import Campaign, CampaignItem, CampaignStatus
from workspaces.models import Workspace

#: What a campaign may set a target against. Declared rather than free text:
#: Phase 7 compares each goal to a measured number, so a goal naming something
#: nothing measures would render as a target that never moves — which reads to
#: a customer as the product being broken rather than the goal being nonsense.
#: Every key here is a column `MetricSnapshot` actually carries.
GOAL_METRICS = frozenset(
    {"reach", "impressions", "likes", "comments", "shares", "saves", "clicks", "engagement_rate"}
)

#: `PLANNING → ACTIVE → CLOSED`. `CLOSED` is absent as a source: it is terminal
#: (see `CampaignStatus`), and a dict with no key for it is a stronger
#: statement than a branch that happens never to be taken.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    CampaignStatus.PLANNING: frozenset({CampaignStatus.ACTIVE}),
    CampaignStatus.ACTIVE: frozenset({CampaignStatus.CLOSED}),
    CampaignStatus.CLOSED: frozenset(),
}


def validate_goals(goals: Any) -> list[dict[str, Any]]:
    if not isinstance(goals, list):
        raise ValidationError({"goals": "Goals are a list of {metric, target} objects."})

    for index, goal in enumerate(goals):
        where = f"Goal {index}"
        if not isinstance(goal, dict):
            raise ValidationError({"goals": f"{where}: each goal is an object."})
        unknown = sorted(set(goal) - {"metric", "target"})
        if unknown:
            raise ValidationError({"goals": f"{where}: unknown key(s) {', '.join(unknown)}."})
        if goal.get("metric") not in GOAL_METRICS:
            raise ValidationError(
                {"goals": f"{where}: unknown metric {goal.get('metric')!r}."},
            )
        target = goal.get("target")
        # `bool` is an `int`; a target of `True` is a mistake, not a number.
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ValidationError({"goals": f"{where}: 'target' must be a number."})
        if target < 0:
            raise ValidationError({"goals": f"{where}: 'target' cannot be negative."})

    return goals


def create_campaign(*, workspace: Workspace, actor: User, **fields: Any) -> Campaign:
    """**Counts every campaign, closed ones included** (P3-04).

    A closed campaign still holds its posts, its goals and — from Phase 7 — its
    digest, so it is still a thing the plan is paying to keep. Excluding it
    would let a workspace sit at a cap of one forever by closing each campaign
    before opening the next, which is not what "one campaign" was sold as.
    """
    entitlements_for(workspace).check_quota(
        "max_campaigns", Campaign.objects.filter(workspace=workspace).count()
    )
    if "goals" in fields:
        validate_goals(fields["goals"])

    campaign = Campaign(workspace=workspace, created_by=actor, **fields)
    campaign.full_clean(exclude=["created_by", "brief"])
    campaign.save()
    return campaign


def set_brief(campaign: Campaign, brief: Post | None) -> Campaign:
    """The brief is a `DOC` in this workspace, or nothing."""
    if brief is not None:
        if brief.workspace_id != campaign.workspace_id:
            # 400 rather than 404: the caller holds this campaign legitimately,
            # and the id they named is refused on its merits. The 404 rule
            # covers *reaching* another tenant's row, which the queryset
            # already prevented.
            raise ValidationError({"brief": "No such document in this workspace."})
        if brief.content_kind != ContentKind.DOC:
            raise ValidationError({"brief": "A brief is a document, not a social post."})

    campaign.brief = brief
    campaign.save(update_fields=["brief", "updated_at"])
    return campaign


def advance(campaign: Campaign, status: str, *, actor: User) -> Campaign:
    """409 on an illegal transition, never 403 (Part 3's error table)."""
    if status not in CampaignStatus.values:
        raise ValidationError({"status": f"Unknown status {status!r}."})

    if status not in ALLOWED_TRANSITIONS[campaign.status]:
        raise StateConflict(
            f"A {campaign.get_status_display().lower()} campaign cannot become {status.lower()}.",
            detail={"from": campaign.status, "to": status},
        )

    campaign.status = status
    fields = ["status", "updated_at"]
    if status == CampaignStatus.CLOSED:
        campaign.closed_at = timezone.now()
        fields.append("closed_at")
    campaign.save(update_fields=fields)

    if status == CampaignStatus.CLOSED:
        _learn_from(campaign)

    return campaign


def _learn_from(campaign: Campaign) -> None:
    """Queue the campaign's read-out (P7-02).

    **An explicit call, not a signal** (Part 7 rule 8): closing a campaign
    producing a digest is a fact about this code path, and a reader of
    `advance` should be able to see it without going looking for a receiver.

    Queued rather than run inline. Learn reads a whole trailing window and may
    sit on a provider for the narration — a close request that waited for it
    would be a request that times out, and the user would close the campaign
    again.

    Deferred import because `learn` imports `planning.models`; at module scope
    this is a cycle. The narrow seam is the price of the dependency running
    that way round, and it runs that way round because Learn knowing about
    campaigns is right while campaigns knowing how to analyse themselves is not.
    """
    from learn.tasks import run_learn_for_campaign

    run_learn_for_campaign.delay(campaign.pk)


def add_post(campaign: Campaign, post: Post, *, actor: User) -> CampaignItem:
    """Idempotent: adding twice is one row, not a conflict.

    Two people dragging the same post onto the same campaign is a race, not a
    disagreement — the same reasoning revoking a review link follows.
    """
    if post.workspace_id != campaign.workspace_id:
        raise ValidationError({"post": "No such post in this workspace."})
    if campaign.status == CampaignStatus.CLOSED:
        raise StateConflict(
            "A closed campaign takes no more posts.",
            detail={"campaign": campaign.pk, "status": campaign.status},
        )

    item, _created = CampaignItem.objects.get_or_create(
        campaign=campaign, post=post, defaults={"added_by": actor}
    )
    return item


def remove_post(campaign: Campaign, post: Post) -> None:
    CampaignItem.objects.filter(campaign=campaign, post=post).delete()
