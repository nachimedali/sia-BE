"""Learn's async entry points (P7-02), on `analyze_q`.

Both tasks are **idempotent in effect rather than in row count**: re-running
writes a new digest rather than editing the last one, because `Digest` is
append-only and a document somebody has already read may not change under them.
A campaign therefore ends up pointing at the newest digest, with every earlier
one still reachable.

Retries land on the next schedule rather than inline. The inputs are a trailing
window, so nothing is lost by waiting, and a provider outage that retried
immediately would become a retry storm against a vendor that is already down.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from billing.services.flags import LEARN_V7, flag_enabled
from learn.services.run import run_learn
from planning.models import Campaign, CampaignStatus

logger = logging.getLogger(__name__)


@shared_task(name="learn.tasks.run_learn_for_campaign")
def run_learn_for_campaign(campaign_id: int) -> int | None:
    """Produce the digest for one campaign. Returns the digest id, or None."""
    campaign = (
        Campaign.objects.select_related("workspace__organization").filter(pk=campaign_id).first()
    )
    if campaign is None:
        return None
    if not flag_enabled(campaign.workspace.organization, LEARN_V7):
        # Flag off is pre-phase behaviour, not an error: closing a campaign
        # closed it and nothing else ran.
        return None

    digest = run_learn(campaign.workspace, campaign=campaign)
    logger.info(
        "learn digest generated",
        extra={"campaign_id": campaign_id, "digest_id": digest.pk},
    )
    return digest.pk


@shared_task(name="learn.tasks.close_due_campaigns")
def close_due_campaigns() -> int:
    """Close campaigns whose end has passed, and learn from each.

    The close is what triggers Learn, so a campaign nobody remembered to close
    would never produce its read-out — the failure mode being avoided is a
    feature that works only for the organised.
    """
    now = timezone.now()
    due = Campaign.objects.filter(status=CampaignStatus.ACTIVE, ends_at__lte=now)

    closed = 0
    for campaign in due.iterator():
        Campaign.objects.filter(pk=campaign.pk, status=CampaignStatus.ACTIVE).update(
            status=CampaignStatus.CLOSED, closed_at=now
        )
        run_learn_for_campaign.delay(campaign.pk)
        closed += 1
    return closed
