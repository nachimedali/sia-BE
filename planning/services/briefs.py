"""The campaign brief (steps-plan S5, S5-01).

A brief is a goal with a number and a window, who it is for, what is offered,
what proves it and what the team can actually do. The plan is written from it
and from nothing else the user did not see.

**Proof is offered, then picked.** The workspace can prove two kinds of thing,
and this module is the one place that says which:

- *Brand Core* — a product claim with its evidence on file (`Product.claims`
  carrying `proof_media`). A claim with no evidence is not offered: the brief
  is where "we say so" turns into "here is why", and a claim nobody can back
  belongs in the user's own words, labelled as theirs.
- *Measured* — the median of the workspace's own published posts over the last
  thirty days, per platform, read through `analysable()` so a fake-adapter row
  can never become a claim (Part 7 rule 17). Below three measured posts there
  is no median to offer — one post is an anecdote, not a baseline.

What the user types is `own_proof`, shown everywhere as "you said".
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from ai.models import CreativeOption
from analytics.services.reporting import latest_per_target
from brand.services.imports import active_core
from channels.models import SocialAccount, SocialAccountStatus
from common.exceptions import StateConflict
from content.models import Platform
from planning.models import Campaign, CampaignBrief, CampaignStatus
from planning.services.campaigns import validate_goals
from products.models import Product
from workspaces.models import Workspace

#: How far back a measured baseline looks, and how many posts make one.
MEASURED_WINDOW_DAYS = 30
MEASURED_FLOOR = 3
#: The metrics a measured baseline is offered for. Summable counts only: a
#: median engagement rate across posts of different sizes reads as a rate the
#: account has, which it does not.
BASELINE_METRICS: tuple[str, ...] = ("impressions", "saves", "clicks")

#: How each metric is said in a sentence.
METRIC_NOUNS: dict[str, str] = {
    "reach": "reach",
    "impressions": "impressions",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saves": "saves",
    "clicks": "link clicks",
    "engagement_rate": "engagement rate",
}

#: What a plan needs before it can be priced, in the order the page lists it.
READINESS: tuple[str, ...] = ("goal", "audience", "offer", "proof", "channels")


class CampaignClosedError(StateConflict):
    default_code = "campaign_closed"
    default_detail = "This campaign is closed. Its brief and plan are kept as they were."


def ensure_open(campaign: Campaign) -> None:
    if campaign.status == CampaignStatus.CLOSED:
        raise CampaignClosedError(detail={"campaign": campaign.pk, "status": campaign.status})


# -----------------------------------------------------------------------------
# Proof
# -----------------------------------------------------------------------------
def measured_medians(workspace: Workspace) -> list[dict[str, Any]]:
    """Per `(platform, metric)`: the median of each measured post's newest
    reading over the window, and how many posts that is.

    Every pair with at least one measured post is returned, with `median`
    null below the floor — the plan says "not enough data: 2 posts", which
    needs the 2.
    """
    ends_at = timezone.now()
    starts_at = ends_at - dt.timedelta(days=MEASURED_WINDOW_DAYS)
    rows = latest_per_target(workspace, starts_at=starts_at, ends_at=ends_at).values(
        "post_target__platform", *BASELINE_METRICS
    )

    readings: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        for metric in BASELINE_METRICS:
            if row[metric] is not None:
                readings[(row["post_target__platform"], metric)].append(row[metric])

    out: list[dict[str, Any]] = []
    for (platform, metric), values in sorted(readings.items()):
        median: float | int | None = None
        if len(values) >= MEASURED_FLOOR:
            raw = statistics.median(values)
            median = int(raw) if float(raw).is_integer() else round(raw, 1)
        out.append({"platform": platform, "metric": metric, "median": median, "n": len(values)})
    return out


def _platform_label(platform: str) -> str:
    try:
        return str(Platform(platform).label)
    except ValueError:
        return platform


def proof_options(workspace: Workspace) -> list[dict[str, str]]:
    """Everything this workspace can put forward as proof, keyed."""
    labels = {
        row.key: row.label for row in CreativeOption.objects.filter(kind="claim", is_active=True)
    }
    options: list[dict[str, str]] = []
    for product in Product.objects.filter(workspace=workspace).order_by("name", "pk"):
        for claim in product.claims or []:
            if not isinstance(claim, dict) or not claim.get("proof_media"):
                continue
            key = str(claim.get("key") or "")
            options.append(
                {
                    "key": f"claim:{product.pk}:{key}",
                    "kind": "core",
                    "text": labels.get(key, key),
                    "source": f"{product.name} · evidence on file",
                }
            )

    for row in measured_medians(workspace):
        if row["median"] is None:
            continue
        noun = METRIC_NOUNS[row["metric"]]
        options.append(
            {
                "key": f"measured:{row['platform']}:{row['metric']}",
                "kind": "measured",
                "text": f"Median {noun} per post on {_platform_label(row['platform'])}: "
                f"{row['median']}",
                "source": f"{row['n']} measured posts, last {MEASURED_WINDOW_DAYS} days",
            }
        )
    return options


def resolved_proof(workspace: Workspace, brief: CampaignBrief) -> list[dict[str, str]]:
    """The proof the plan may use: picked keys that still resolve, then the
    user's own. A key that no longer resolves — the claim lost its evidence,
    the median fell below the floor — is dropped, never shown from a stale
    copy."""
    available = {row["key"]: row for row in proof_options(workspace)}
    picked = [available[key] for key in brief.proof_keys if key in available]
    own = [
        {"key": f"you:{index}", "kind": "you", "text": text, "source": "You said, in the brief"}
        for index, text in enumerate(brief.own_proof)
    ]
    return picked + own


# -----------------------------------------------------------------------------
# The brief
# -----------------------------------------------------------------------------
def brief_of(campaign: Campaign) -> CampaignBrief:
    """The saved brief, or an unsaved blank one — a read never writes a row."""
    try:
        return campaign.brief_form
    except CampaignBrief.DoesNotExist:
        return CampaignBrief(campaign=campaign)


def goal_of(campaign: Campaign) -> dict[str, Any] | None:
    return campaign.goals[0] if campaign.goals else None


def missing(campaign: Campaign, brief: CampaignBrief) -> list[str]:
    goal = goal_of(campaign)
    constraints = brief.constraints or {}
    checks = {
        "goal": bool(goal and goal.get("target")),
        "audience": bool(brief.audience.strip()),
        "offer": bool(brief.offer.strip()),
        "proof": bool(brief.proof_keys or brief.own_proof),
        "channels": bool(constraints.get("channels")) and bool(constraints.get("posts_per_week")),
    }
    return [key for key in READINESS if not checks[key]]


def connected_channels(workspace: Workspace) -> list[str]:
    return sorted(
        set(
            SocialAccount.objects.filter(
                workspace=workspace, status=SocialAccountStatus.ACTIVE
            ).values_list("platform", flat=True)
        )
    )


def voice_of(workspace: Workspace) -> str:
    """The Brand Core voice preset's name, or blank — the plan says whose voice
    it was written in, so a brand with none is told so rather than given one."""
    core = active_core(workspace)
    if core is None:
        return ""
    voice = (core.sections.get("voice") or {}).get("value") or {}
    preset = str(voice.get("preset") or "")
    if not preset:
        return ""
    row = CreativeOption.objects.filter(kind="tone_preset", key=preset).first()
    return row.label if row else preset


@transaction.atomic
def save(campaign: Campaign, data: dict[str, Any], *, actor: User) -> CampaignBrief:
    """Partial: only the keys present change. Validated whole before anything
    is written, so a bad field saves nothing."""
    ensure_open(campaign)
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    brief = brief_of(campaign)

    if "goal" in data:
        goal = data["goal"]
        campaign.goals = validate_goals([goal]) if goal else []

    starts_at = data.get("starts_at", campaign.starts_at)
    ends_at = data.get("ends_at", campaign.ends_at)
    if ends_at <= starts_at:
        raise ValidationError({"ends_at": "A campaign ends after it starts."})
    campaign.starts_at, campaign.ends_at = starts_at, ends_at

    if "proof_keys" in data:
        available = {row["key"] for row in proof_options(campaign.workspace)}
        unknown = [key for key in data["proof_keys"] if key not in available]
        if unknown:
            raise ValidationError({"proof_keys": f"No such proof: {', '.join(unknown)}."})

    for field in ("audience", "segments", "offer", "proof_keys", "own_proof", "constraints"):
        if field in data:
            setattr(brief, field, data[field])

    campaign.save(update_fields=["goals", "starts_at", "ends_at", "updated_at"])
    brief.campaign = campaign
    brief.updated_by = actor
    brief.save()
    return brief


def read(campaign: Campaign) -> dict[str, Any]:
    brief = brief_of(campaign)
    gaps = missing(campaign, brief)
    return {
        "goal": goal_of(campaign),
        "starts_at": campaign.starts_at,
        "ends_at": campaign.ends_at,
        "audience": brief.audience,
        "segments": brief.segments,
        "offer": brief.offer,
        "proof_keys": brief.proof_keys,
        "own_proof": brief.own_proof,
        "constraints": brief.constraints,
        "proof_options": proof_options(campaign.workspace),
        "connected_channels": connected_channels(campaign.workspace),
        "voice": voice_of(campaign.workspace),
        "missing": gaps,
        "ready": not gaps,
        "updated_at": brief.updated_at,
    }
