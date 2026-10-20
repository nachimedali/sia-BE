"""What the inbox lists and counts (S2-01).

Everything starts from `analytics.services.audience.comments_for`, so the
inbox obeys the same measured-only and plan-horizon rules as the sentiment
aggregate: a comment older than the plan keeps never appears here, and a
platform that cannot report is never counted as having no comments.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Exists, OuterRef, Prefetch, Q, QuerySet

from analytics.models import AudienceComment, AudienceCommentAction, Availability, CommentAction
from analytics.services import audience
from analytics.services.ingest import UNAVAILABLE_MARKER
from billing.services.entitlements import entitlements_for
from content.models import PostTarget, PostTargetState
from inbox.models import Intent, TriageStatus
from workspaces.models import Workspace

UNCODED = "uncoded"
SEARCH_MAX = 100


def _actions(with_actors: bool) -> Any:
    """What was done to each comment. The detail also prints who did it, so it
    selects them with the actions rather than one query per reply."""
    rows = AudienceCommentAction.objects.all()
    return Prefetch("actions", rows.select_related("actor") if with_actors else rows)


def base(workspace: Workspace, *, with_actors: bool = False) -> QuerySet[AudienceComment]:
    deleted = AudienceCommentAction.objects.filter(
        comment=OuterRef("pk"), action=CommentAction.DELETE
    )
    return (
        audience.comments_for(workspace)
        .exclude(Exists(deleted))
        .select_related("post_target", "post_target__post", "coding", "triage", "triage__assignee")
        .prefetch_related(_actions(with_actors))
        .annotate(note_count=Count("notes", distinct=True))
    )


def apply_filters(
    queryset: QuerySet[AudienceComment], params: Any, *, user: Any
) -> QuerySet[AudienceComment]:
    if platform := params.get("platform"):
        queryset = queryset.filter(post_target__platform=platform)
    if post := params.get("post"):
        queryset = queryset.filter(post_target__post_id=post)
    if sentiment := params.get("sentiment"):
        queryset = queryset.filter(sentiment=sentiment)
    if intent := params.get("intent"):
        if intent == UNCODED:
            queryset = queryset.filter(Q(coding__isnull=True) | Q(coding__intent__isnull=True))
        else:
            queryset = queryset.filter(coding__intent=intent)
    if status := params.get("status"):
        if status == TriageStatus.DONE:
            queryset = queryset.filter(triage__status=TriageStatus.DONE)
        else:
            queryset = queryset.filter(Q(triage__isnull=True) | Q(triage__status=TriageStatus.OPEN))
    if assignee := params.get("assignee"):
        if assignee == "me":
            queryset = queryset.filter(triage__assignee=user)
        elif assignee == "none":
            queryset = queryset.filter(Q(triage__isnull=True) | Q(triage__assignee__isnull=True))
        elif str(assignee).isdigit():
            queryset = queryset.filter(triage__assignee_id=int(assignee))
    if (replied := params.get("replied")) in ("yes", "no"):
        has_reply = Exists(
            AudienceCommentAction.objects.filter(comment=OuterRef("pk"), action=CommentAction.REPLY)
        )
        queryset = queryset.filter(has_reply) if replied == "yes" else queryset.exclude(has_reply)
    if search := (params.get("q") or "").strip()[:SEARCH_MAX]:
        queryset = queryset.filter(Q(body__icontains=search) | Q(author__icontains=search))
    return queryset.order_by("-posted_at", "-id")


def one(workspace: Workspace, pk: int) -> AudienceComment | None:
    return base(workspace, with_actors=True).filter(pk=pk).first()


def platform_coverage(workspace: Workspace) -> list[dict[str, Any]]:
    """Per platform with a published copy: can it report comments at all?

    `unavailable` (TikTok, at any price) and `none_yet` (it can, nobody has
    written) are different answers and must never share a rendering — the
    first is a limit of the vendor, the second is news about the audience.
    """
    targets = PostTarget.objects.filter(post__workspace=workspace, state=PostTargetState.PUBLISHED)
    platforms = sorted(set(targets.values_list("platform", flat=True)))
    unavailable = set(
        AudienceComment.objects.filter(
            post_target__in=targets,
            availability=Availability.UNAVAILABLE,
            external_id__startswith=UNAVAILABLE_MARKER,
        ).values_list("post_target__platform", flat=True)
    )
    counts = dict(
        audience.comments_for(workspace)
        .order_by()
        .values_list("post_target__platform")
        .annotate(n=Count("id"))
    )
    rows = []
    for platform in platforms:
        if platform in unavailable:
            state = "unavailable"
        elif counts.get(platform):
            state = "available"
        else:
            state = "none_yet"
        rows.append({"platform": platform, "state": state, "comments": counts.get(platform, 0)})
    return rows


def summary(workspace: Workspace, *, user: Any) -> dict[str, Any]:
    """Counted by the database, not in Python: this runs on every edit."""
    rows = base(workspace).order_by()
    totals = rows.aggregate(
        total=Count("id"),
        done=Count("id", filter=Q(triage__status=TriageStatus.DONE)),
        unassigned=Count("id", filter=Q(triage__assignee__isnull=True)),
        mine=Count("id", filter=Q(triage__assignee=user)),
    )
    intents = dict(rows.values_list("coding__intent").annotate(n=Count("id")))
    uncoded = intents.pop(None, 0)
    return {
        "total": totals["total"],
        "open": totals["total"] - totals["done"],
        "done": totals["done"],
        "unassigned": totals["unassigned"],
        "mine": totals["mine"],
        "intents": {**{key: intents.get(key, 0) for key in Intent.values}, UNCODED: uncoded},
        "sentiments": dict(rows.values_list("sentiment").annotate(n=Count("id"))),
        "platforms": platform_coverage(workspace),
        "can_reply": entitlements_for(workspace).feature(audience.REPLY_FEATURE),
    }
