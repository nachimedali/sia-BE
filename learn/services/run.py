"""The Learn job (P7-02) — the one entry point that produces a digest.

Triggered at campaign close and on demand. Everything it needs is already
captured; this module sequences it and writes the result down:

    observations  →  statistics  →  Digest + Findings  →  narration  →  proposals

**The window is trailing and the campaign is only the scope** (P7-05), and the
horizon is the plan's, checked the same way every other analytics reader checks
it — a digest may not reach further back than the plan retains.

Ordering note: the findings are written **before** narration runs. A provider
that hangs or misbehaves then costs the prose, which has a template fallback,
rather than the statistics, which would have to be recomputed. The digest is
created first for the same reason its findings point at it.

The narration call itself runs **outside any transaction** — it is network
I/O to the text-generation provider, and holding a DB transaction open across
it would keep a connection (and the digest/finding row locks) idle for as
long as the provider takes to answer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import transaction
from django.utils import timezone

from ai.providers.llm_text import get_text_provider
from billing.services.entitlements import entitlements_for
from learn.models import Digest, Finding
from learn.services import narrate, proposals, segments, statistics
from planning.models import Campaign
from taste.models import RuleSet, TasteProfile


def _window(
    workspace: Any, campaign: Campaign | None, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    """The statistical window: the plan's horizon, ending now.

    Deliberately **not** the campaign's own dates even when a campaign is
    given. A two-week campaign at three posts a week is n=6 — under the
    Emerging floor — so statistics bounded by the campaign would grade every
    line "Insufficient" and the feature would look broken on the day it
    shipped. The campaign scopes *what the digest is about*; the trailing
    window is what it is computed from.
    """
    horizon = entitlements_for(workspace).analytics_horizon_days()
    end = now
    if campaign is not None and campaign.closed_at:
        # A campaign closed in the past reads to its close, not to today: a
        # sprint analysed weeks later would otherwise absorb everything
        # published since and attribute it to the sprint.
        end = max(campaign.closed_at, campaign.starts_at)
    return end - dt.timedelta(days=horizon), end


def run_learn(
    workspace: Any,
    *,
    campaign: Campaign | None = None,
    requested_by: Any = None,
    now: dt.datetime | None = None,
    provider: Any = None,
) -> Digest:
    """Produce one digest. Always writes a row, even when there is nothing yet.

    An empty digest is a real answer — "we looked, and there is not enough to
    say" — and a customer who closed a campaign needs to see that rather than
    an absence they cannot distinguish from a failed job.
    """
    moment = now or timezone.now()
    window_start, window_end = _window(workspace, campaign, moment)

    rows = segments.observations(
        workspace.pk,
        horizon_days=max(0, (window_end - window_start).days),
        timezone_name=getattr(workspace, "timezone", "UTC") or "UTC",
        now=window_end,
    )
    stats = statistics.analyse(rows)

    active_profile = (
        TasteProfile.objects.filter(workspace=workspace, is_active=True)
        .values_list("version", flat=True)
        .first()
    )
    active_ruleset = (
        RuleSet.objects.filter(workspace=workspace, is_active=True)
        .values_list("version", flat=True)
        .first()
    )

    payload = narrate.build_payload(
        stats,
        window_start=window_start,
        window_end=window_end,
        campaign_name=campaign.name if campaign else "",
    )

    with transaction.atomic():
        digest = Digest.objects.create(
            workspace=workspace,
            campaign=campaign,
            window_start=window_start,
            window_end=window_end,
            statistics=payload,
            taste_profile_version=active_profile,
            ruleset_version=active_ruleset,
            requested_by=proposals.actor_or_none(requested_by),
        )

        findings = Finding.objects.bulk_create(
            [
                Finding(
                    digest=digest,
                    segment={"dimension": row.dimension, "value": row.value},
                    comparison=row.as_payload(),
                    confidence=row.confidence,
                    sample_size=row.sample_size,
                    baseline_size=row.baseline_size,
                    campaigns_observed=row.campaigns_observed,
                    excluded_reason=row.excluded_reason,
                )
                for row in stats
            ]
        )

    # Outside the transaction above: this is network I/O to the narration
    # provider, and a DB transaction has no business staying open across it.
    text, source = narrate.narrate(payload, provider=provider or get_text_provider())

    with transaction.atomic():
        # `Digest` is append-only, so the narration lands through a queryset
        # update rather than `save()`. The guard is there to stop a *semantic*
        # rewrite of a document somebody has read; this is the same job
        # finishing the row it only just wrote.
        Digest.objects.filter(pk=digest.pk).update(narration=text, narration_source=source)
        digest.narration = text
        digest.narration_source = source

        proposals.propose_from(digest, findings=findings)

        if campaign is not None:
            # Re-running repoints the campaign at the newer digest and leaves
            # the older one readable — both are documents somebody may have
            # opened.
            Campaign.objects.filter(pk=campaign.pk).update(digest=digest)

    return digest
