"""Coding new comments (S2-02), on `ai_q` — a pass may ask the text model."""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from django.utils import timezone

from analytics.models import AudienceComment, Availability
from billing.services.flags import INBOX_S2, flag_enabled
from inbox.services import coding

#: One pass looks at this many uncoded comments, newest first, so a backlog is
#: worked down over several runs instead of one that holds a worker for minutes.
BATCH = 200
#: Comments older than this are not worth a model call on first sight.
LOOKBACK_DAYS = 30


@shared_task(name="inbox.tasks.code_new_comments")
def code_new_comments() -> int:
    """Code comments that have no coding row, in organizations with the flag on."""
    cutoff = timezone.now() - dt.timedelta(days=LOOKBACK_DAYS)
    candidates = list(
        AudienceComment.objects.filter(
            availability=Availability.MEASURED, coding__isnull=True, posted_at__gte=cutoff
        )
        .select_related("post_target__post__workspace__organization")
        .order_by("-posted_at")[:BATCH]
    )
    allowed: dict[int, bool] = {}
    ready = []
    for comment in candidates:
        organization = comment.post_target.post.workspace.organization
        key = organization.pk if organization else 0
        if key not in allowed:
            allowed[key] = flag_enabled(organization, INBOX_S2)
        if allowed[key]:
            ready.append(comment)
    return coding.code_comments(ready)
